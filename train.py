"""
This single file is intended to perform some magic for training/finetuning.
"""

import gc
import os
import time
import math
import pickle
import random
import logging
import datetime
import dataclasses
from contextlib import nullcontext

import numpy as np
import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

from model import AllamoTransformerConfig, AllamoTransformer
from configuration import AllamoConfiguration
from train_utils import (
    create_dataloader,
    remove_unwanted_prefix_from_model_state_dict,
    get_lr,
    get_grad_accum,
    format_seconds_as_time,
    calculate_eta,
    has_next_iter_to_perform,
    estimate_mfu,
)

class AllamoTrainer:

    def __init__(self, config: AllamoConfiguration, ddp=False):
        self.config = config
        self.ddp = ddp
        self.__init_torch(config)
        self.__init_logger(config)
        
        self.iter_num = 0
        self.best_train_loss = 1e2
        self.best_val_loss = 1e2
        self.processed_tokens = 0
        self.data_loader = create_dataloader(config, self.ddp_rank, self.ddp_world_size)
        self.__init_training(config)
        
    def __init_logger(self, config: AllamoConfiguration):
        run_timestamp_str = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
        if self.ddp:
            log_file_name_base = f'train-{run_timestamp_str}-rank_{self.ddp_rank}'
        else:
            log_file_name_base = f'train-{run_timestamp_str}'
        log_file_path = os.path.join(config.out_dir, f'{log_file_name_base}.log')
        logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(), logging.FileHandler(log_file_path)])
        self.logger = logging.getLogger('AllamoTrainer')
            
    def __init_torch(self, config: AllamoConfiguration):
        if self.ddp:
            dist.init_process_group(backend=config.backend)
            self.ddp_rank = int(os.environ['RANK'])
            self.ddp_local_rank = int(os.environ['LOCAL_RANK'])
            self.ddp_world_size = int(os.environ['WORLD_SIZE'])
            print(
                f"RANK: {self.ddp_rank}, LOCAL_RANK: {self.ddp_local_rank}, "
                f"WORLD_SIZE: {self.ddp_world_size}, LOCAL_WORLD_SIZE: {os.environ['LOCAL_WORLD_SIZE']}"
            )
            config.device = f'cuda:{self.ddp_local_rank}'
            torch.cuda.set_device(config.device)
            self.master_process = self.ddp_rank == 0 # this process will do logging, checkpointing etc.
            self.seed_offset = self.ddp_rank # each process gets a different seed
        else:
            # if not ddp, we are running on a single gpu, and one process
            self.ddp_rank = 0
            self.ddp_local_rank = None
            self.ddp_world_size = 1
            self.master_process = True
            self.seed_offset = 0
    
        if self.master_process:
            os.makedirs(config.out_dir, exist_ok=True)
        torch.manual_seed(config.seed + self.seed_offset)
        torch.cuda.manual_seed(config.seed + self.seed_offset)
        torch.backends.cuda.matmul.allow_tf32 = True # allow tf32 on matmul
        torch.backends.cudnn.allow_tf32 = True # allow tf32 on cudnn
        torch.set_float32_matmul_precision("highest") # set to "high" for faster matrix multiplications with bfloat16
        ptdtype = {'float32': torch.float32, 'bfloat16': torch.bfloat16, 'bfloat16-true': torch.bfloat16, 'float16': torch.float16}[config.dtype]
        self.device_type = 'cuda' if 'cuda' in config.device else 'cpu' # for later use in torch.autocast
        self.ctx = nullcontext() if self.device_type == 'cpu' else torch.amp.autocast(device_type=self.device_type, dtype=ptdtype)
        if config.dtype == 'bfloat16-true':
            # torch.set_float32_matmul_precision("high")
            torch.set_default_dtype(torch.bfloat16)
        
    def __init_training(self, config: AllamoConfiguration):
        transformer_config_fields = [f.name for f in dataclasses.fields(AllamoTransformerConfig)]
        checkpoint_name = None
        if config.init_from == 'resume':
            checkpoint_name = 'ckpt.pt'
        elif config.init_from == 'resume_last':
            checkpoint_name = 'last_eval_ckpt.pt'
        else:
            if os.path.exists(os.path.join(config.out_dir, 'config_ckpt.pt')) \
                or os.path.exists(os.path.join(config.out_dir, 'model_ckpt.pt')) \
                or os.path.exists(os.path.join(config.out_dir, 'optimizer_ckpt.pt')):
                self.logger.info("Delete existing checkpoint files to start from scratch or use --init_from=resume to resume training")
                exit()
            
        if checkpoint_name is not None:
            self.logger.info(f"Resuming training from {config.out_dir}")
            # resume training from a checkpoint
            ckpt_dir = config.checkpoint_path if config.checkpoint_path else config.out_dir
            self.logger.info(f"Loading {checkpoint_name} checkpoint files from {ckpt_dir}...")
            config_checkpoint = torch.load(os.path.join(ckpt_dir, f'config_{checkpoint_name}'), map_location='cpu')
            checkpoint_model_args = config_checkpoint['model_args']
            # force these config attributes to be equal otherwise we can't even resume training
            # the rest of the attributes (e.g. dropout) can stay as desired from command line
            for k in transformer_config_fields:
                if hasattr(config, k) and hasattr(checkpoint_model_args, k):
                    setattr(config, k, getattr(checkpoint_model_args, k))
            if 'iter_num' in config_checkpoint:
                self.iter_num = config_checkpoint['iter_num']
            if 'best_train_loss' in config_checkpoint:
                self.best_train_loss = config_checkpoint['best_train_loss']
            if 'best_val_loss' in config_checkpoint:
                self.best_val_loss = config_checkpoint['best_val_loss']
            if 'processed_tokens' in config_checkpoint:
                self.processed_tokens = config_checkpoint['processed_tokens']
                
            if config.dataloader_type == 'allamo':
                if  'allamo_dataloader_train_processed_files' in config_checkpoint:
                    self.data_loader.train_dataset.processed_files = config_checkpoint['allamo_dataloader_train_processed_files']
                    if len(self.data_loader.train_dataset.processed_files) > 0:
                        # Removing the last element from the list because it represents the file where processing was interrupted.
                        # We will load this file and resume processing from there, indicated by the dataset_offset.
                        self.data_loader.train_dataset.processed_files.pop()
                        self.data_loader.train_dataset.load_next_dataset()
                if 'allamo_dataloader_dataset_offset' in config_checkpoint:
                    self.data_loader.dataset_offset = config_checkpoint['allamo_dataloader_dataset_offset']
                if 'allamo_dataloader_epoch' in config_checkpoint:
                    self.data_loader.epoch = config_checkpoint['allamo_dataloader_epoch']    
            del config_checkpoint
            del checkpoint_model_args
            
        model_args = {k: getattr(config, k) for k in transformer_config_fields if hasattr(config, k)}
        modelConf = AllamoTransformerConfig(**model_args)
        model = AllamoTransformer(modelConf)
        self.model_num_params = model.model_num_params
        if checkpoint_name is None:
            self.logger.info("Initialized a new model from scratch")
        else:
            self.load_model_checkpoint(model, os.path.join(ckpt_dir, f'model_{checkpoint_name}'))
        model.to(config.device)

        # compile the model - requires PyTorch 2.0
        if config.compile:
            self.logger.info("compiling the model... (takes a ~minute)")
            try:
                model = torch.compile(model, mode=config.compile_mode)
                self.logger.info("Model compiled and ready to use")
            except Exception as err:
                self.logger.warn(f"Model compile not supported: {err}")

        self.raw_model = model # neeeded in DDP training
        self.model = model
        # wrap model into DDP container
        if self.ddp:
            self.model = DDP(self.model, device_ids=[self.ddp_local_rank])
            
        # initialize a GradScaler. If enabled=False scaler is a no-op
        self.scaler = torch.cuda.amp.GradScaler(enabled=(config.dtype == 'float16' or config.dtype == 'bfloat16'))
        
        # optimizer
        self.optimizer = self.model.configure_optimizers(config, self.device_type)
        if checkpoint_name is not None:
            self.load_optimizer_checkpoint(self.optimizer, os.path.join(ckpt_dir, f'optimizer_{checkpoint_name}'))
                
        # gradient_accumulation scheduler
        if config.grad_accum_schedule: 
            config.grad_accum_max = config.gradient_accumulation_steps
            config.gradient_accumulation_steps = config.grad_accum_initial
            self.logger.info(
                f"Gradient accumulation scheduler enabled. "
                f"Current gradient accumulation steps: {config.gradient_accumulation_steps}"
            )
        self.gradient_accumulation_steps = config.gradient_accumulation_steps
        
        if config.decay_lr:
            self.logger.info(f"Cosing decay learning rate enabled. Currect learning rate: {get_lr(self.iter_num, self.config)}")
        else:
            self.logger.info(f"Using constant learning rate: {config.learning_rate}")
            
    def load_model_checkpoint(self, model, ckpt_path):
        state_dict = torch.load(ckpt_path, map_location='cpu')
        remove_unwanted_prefix_from_model_state_dict(state_dict)
        model.load_state_dict(state_dict)
        self.logger.info("Loaded model from the checkpoint")
        
    def load_optimizer_checkpoint(self, optimizer, ckpt_path):
        if os.path.exists(ckpt_path):
            state_dict = torch.load(ckpt_path, map_location=config.device)
            optimizer.load_state_dict(state_dict)
            self.logger.info("Optimizer state loaded.")
        else:
            self.logger.warning("Optimizer checkpoint file not found. Initializing optimizer from scratch")

    # helps estimate an arbitrarily accurate loss over either split using many batches
    @torch.no_grad()
    def estimate_loss(self):
        losses_out = {}
        accuraces = {}
        self.model.eval()
        for split in self.data_loader.splits:
            losses = torch.zeros(self.config.eval_iters)
            correct_preds = 0
            total_preds = 0
            for k in range(self.config.eval_iters):
                X, Y = self.data_loader.get_batch(split, True)
                with self.ctx:
                    logits, loss, _ = self.model(X, Y)
                losses[k] = loss.item()
                total_preds += Y.size(0)
                correct_preds += (logits[:,-1,:].max(1).indices == Y[:,-1]).sum().item()
            losses_out[split] = losses.mean()
            accuraces[split] = correct_preds / total_preds
        self.model.train()
        if 'val' not in losses_out:
            losses_out['val'] = losses_out['train']
            accuraces['val'] = accuraces['train']
        return losses_out, accuraces

    # helps saving checkpoint to a file
    def save_checkpoint(self, ckpt_file_name):
        checkpoint = {
            'model_args': self.raw_model.config,
            'iter_num': self.iter_num,
            'best_train_loss': self.best_train_loss,
            'best_val_loss': self.best_val_loss,
            'processed_tokens': self.processed_tokens,
            'config': self.config.__dict__,
        }
        if config.dataloader_type == 'allamo':
            checkpoint['allamo_dataloader_train_processed_files'] = self.data_loader.train_dataset.processed_files
            checkpoint['allamo_dataloader_dataset_offset'] = self.data_loader.dataset_offset
            checkpoint['allamo_dataloader_epoch'] = self.data_loader.epoch
        
        ckpt_file_path = os.path.join(self.config.out_dir, 'config_' + ckpt_file_name)
        self.logger.info(f"saving config checkpoint to {ckpt_file_path}")
        torch.save(checkpoint, ckpt_file_path)
        
        ckpt_file_path = os.path.join(self.config.out_dir, 'model_' + ckpt_file_name)
        self.logger.info(f"saving model checkpoint to {ckpt_file_path}")
        torch.save(self.raw_model.state_dict(), ckpt_file_path)
        
        ckpt_file_path = os.path.join(self.config.out_dir, 'optimizer_' + ckpt_file_name)
        self.logger.info(f"saving optimizer checkpoint to {ckpt_file_path}")
        torch.save(self.optimizer.state_dict(), ckpt_file_path)
        self.logger.info(f"checkpoint files saved in {config.out_dir}")
        
    def train(self):
        self.logger.info(f"Starting training with configuration: {self.config}")
        X, Y = self.data_loader.get_batch('train') # fetch the very first batch
        self.start_iter = self.iter_num
        self.start_timestamp = datetime.datetime.now()
        current_epoch = self.data_loader.epoch
        while has_next_iter_to_perform(self.iter_num, self.config, self.data_loader):
            if current_epoch < self.data_loader.epoch:
                self.save_checkpoint(f'epoch_{current_epoch}.pt', model_only=True)
                current_epoch = self.data_loader.epoch
            
            timer = time.time()
            log_iter = (self.iter_num % self.config.log_interval == 0 and self.master_process)
            eval_iter = (self.iter_num % self.config.eval_interval == 0 and self.master_process)
            lr = get_lr(self.iter_num, self.config)
            for param_group in self.optimizer.param_groups:
                param_group['lr'] = lr
                
            # determine and set batch_size and gradient_accumulation_steps for this iteration 
            micro_batch_size = self.data_loader.update_batch_size(self.iter_num)
            self.gradient_accumulation_steps = get_grad_accum(self.gradient_accumulation_steps, self.iter_num, self.config)
            total_batch_size = self.config.block_size * micro_batch_size * self.gradient_accumulation_steps
            if self.ddp:
                total_batch_size *= self.ddp_world_size

            # evaluate the loss on train/val sets and write checkpoints
            if eval_iter:
                eval_time = time.time()
                losses, accuraces = self.estimate_loss()
                eval_time = time.time() - eval_time
                train_loss = losses['train']
                val_loss = losses['val']
                train_ppl = torch.exp(train_loss)
                val_ppl = torch.exp(val_loss)
                self.logger.info(
                    f"iter {self.iter_num:,}: train loss={train_loss:.4f} ppl={train_ppl:.4f} "
                    f"acc={accuraces['train']:.4f} (best loss={self.best_train_loss:.4f}), "
                    f"val loss={val_loss:.4f} ppl={val_ppl:.4f} acc={accuraces['val']:.4f} "
                    f"(best loss={self.best_val_loss:.4f}), tokens {self.processed_tokens:,}"
                )
                if self.iter_num > self.start_iter:
                    if losses['train'] < self.best_train_loss:
                        self.best_train_loss = losses['train']
                    if losses['val'] < self.best_val_loss:
                        self.best_val_loss = losses['val']
                        self.save_checkpoint('ckpt.pt')
                    if self.config.always_save_checkpoint:
                        self.save_checkpoint('last_eval_ckpt.pt')
                if self.config.wandb_log:
                    wandb.log({
                        "iter": self.iter_num,
                        "eval/time": eval_time*1000,
                        "eval/samples_per_second": (self.config.eval_iters * len(self.data_loader.splits)) / eval_time,
                        "eval/train_loss": train_loss,
                        "eval/val_loss": val_loss,
                        "eval/train_ppl": train_ppl,
                        "eval/val_ppl": val_ppl,
                        "eval/train_acc": accuraces['train'],
                        "eval/val_acc": accuraces['val'],
                        "eval/diff_loss": (val_loss-train_loss),
                        "eval/diff_acc": (accuraces['train']-accuraces['val']),
                        "eval/diff_ppl": (val_ppl-train_ppl),
                        "eval/best_train_loss": self.best_train_loss,
                        "eval/best_val_loss": self.best_val_loss
                    })
                gc.collect()
                torch.cuda.empty_cache()
            
            if self.config.eval_only:
                break
            
            # numpy.memmap does not release RAM after reading data. To keep memory consumption low, let's reconstruct the memmap objects
            if self.config.reload_datasets_interval > 0 and self.iter_num % self.config.reload_datasets_interval == 0:
                self.data_loader.reload_datasets()
                gc.collect()
                torch.cuda.empty_cache()
            
            accuracy = 0
            batch_mfu_excluded_time = 0
            fwdbwd_time = time.time()
            # forward backward update, with optional gradient accumulation to simulate larger batch size
            # and using the GradScaler if data type is float16
            micro_steps = self.gradient_accumulation_steps
            for micro_step in range(self.gradient_accumulation_steps):
                if self.ddp:
                    # in DDP training we only need to sync gradients at the last micro step.
                    # the official way to do this is with model.no_sync() context manager, but
                    # I really dislike that this bloats the code and forces us to repeat code
                    # looking at the source of that context manager, it just toggles this variable
                    self.model.require_backward_grad_sync = (micro_step == self.gradient_accumulation_steps - 1)
                with self.ctx:
                    logits, loss, _ = self.model(X, Y)
                    if micro_steps > 1:
                        loss = loss / micro_steps # scale the loss to account for micro steps
                
                mfu_excluded_time = time.time()
                # count processed tokens
                self.processed_tokens += X.numel() * self.ddp_world_size if self.ddp else X.numel()
                if log_iter and (micro_step == self.gradient_accumulation_steps - 1):
                    # calculate accuracy. note: this is a CPU-GPU sync point!
                    accuracy = (logits.max(2).indices == Y).sum().item() / Y.view(-1).size(0)
                # immediately async prefetch next batch while model is doing the forward pass on the GPU
                X, Y = self.data_loader.get_batch('train')
                batch_mfu_excluded_time += time.time() - mfu_excluded_time
                
                # backward pass, with gradient scaling if training in fp16
                self.scaler.scale(loss).backward()
                
            # clip the gradient
            if self.config.grad_clip != 0.0:
                self.scaler.unscale_(self.optimizer)
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.config.grad_clip)
            # step the optimizer and scaler if training in fp16
            self.scaler.step(self.optimizer)
            self.scaler.update()
            # flush the gradients as soon as we can, no need for this memory anymore
            self.optimizer.zero_grad(set_to_none=True)

            # timing and logging
            if log_iter:
                fwdbwd_time = time.time() - fwdbwd_time - batch_mfu_excluded_time
                iter_time = time.time() - timer
                # get loss as float. note: this is a CPU-GPU sync point
                # scale up to undo the division above, approximating the true total loss (exact would have been a sum)
                lossf = loss.item() * micro_steps
                ppl = torch.exp(torch.tensor(lossf))
                if self.config.mfu_flops_peak > 0 and self.iter_num > self.start_iter:
                    mfu = estimate_mfu(self.model_num_params, self.config, micro_batch_size * self.gradient_accumulation_steps, fwdbwd_time)
                    mfu_str = f'{mfu*100:.2f}%'
                else:
                    mfu = -1.0
                    mfu_str = 'n/a'
                mtu = fwdbwd_time/iter_time # model time utilization
                iter_time_ms = iter_time * 1000
                self.logger.info(
                    f"iter {self.iter_num:,}: loss {lossf:.4f}, ppl {ppl:.4f}, acc {accuracy:.4f}, "
                    f"iter time {iter_time_ms:.2f}ms, tokens {self.processed_tokens:,}, lr {lr:.6f}, "
                    f"mfu {mfu_str}, mtu {mtu*100:.2f}%, epoch {self.data_loader.epoch}, "
                    f"ETA: {calculate_eta(self.iter_num, self.start_iter, self.start_timestamp, self.config)}"
                )
                if self.config.wandb_log:
                    metrics = {
                        "iter": self.iter_num,
                        "train/iter_time": iter_time_ms,
                        "train/loss": lossf,
                        "train/ppl": ppl,
                        "train/acc": accuracy,
                        "train/lr": lr,
                        "train/tokens": self.processed_tokens,
                        "train/tokens_per_sec": (total_batch_size/iter_time),
                        "train/tokens_per_gpu_per_sec": (total_batch_size/self.ddp_world_size/iter_time),
                        "train/total_batch_size": total_batch_size,
                        "train/mtu": mtu,
                        "train/epoch": self.data_loader.epoch
                    }
                    if mfu > 0:
                        metrics['train/mfu'] = mfu
                    if self.config.dataset_seq_train:
                        metrics['train/ds_offset'] = self.data_loader.dataset_offset
                    wandb.log(metrics)
            self.iter_num += 1
            
        training_time = format_seconds_as_time((datetime.datetime.now() - self.start_timestamp).total_seconds())
        self.logger.info(f"Training finished in {training_time}")
        
        if self.master_process and not self.config.eval_only:
            self.save_checkpoint('final_ckpt.pt')

if __name__ == '__main__':
    ddp = int(os.environ.get('RANK', -1)) != -1 # is this a ddp run?
    config = AllamoConfiguration()
    trainer = AllamoTrainer(config, ddp)
    
    # logging
    if config.wandb_log and trainer.master_process:
        import wandb
        wandb_run_name = config.wandb_run_name + datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        wandb.init(project=config.wandb_project, name=wandb_run_name, config=config)
    
    # clean up after initialization
    gc.collect()
    torch.cuda.empty_cache()
    
    trainer.train()  
      
    if ddp:
        dist.destroy_process_group()
