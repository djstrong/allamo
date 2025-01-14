import gc
import glob
import joblib
import logging
import numpy as np
import os
import threading
import time
import torch
import torch.nn.functional as F
from configuration import AllamoConfiguration

class AllamoDataset:
    """ In-Memory map-style dataset """

    def __init__(self, config: AllamoConfiguration, train_split=True, rank=None, world_size=None):
        self.logger = logging.getLogger('AllamoDataset')
        self.rank = rank
        self.world_size = world_size
        self.data_dir = config.data_dir
        self.block_size = config.block_size
        self.sample_size = config.block_size + 1 
        self.ignore_index = config.ignore_index
        self.pad_token_id = config.pad_token_id
        self.weighted_loss = config.weighted_loss
        self.training_type = config.training_type
        self.data = None
        self.data_in_alm_format = False
        self.dataset_files = self.get_dataset_files(config, train_split)
        self.processed_files = []
        if config.dataset_train_processed_files_count > 0:
            self.processed_files = self.dataset_files[:config.dataset_train_processed_files_count]
        self.load_next_dataset()
        
    def get_dataset_files(self, config, train_split):
        dataset_files = []
        if train_split and config.dataset_train_files:
            dataset_files = config.dataset_train_files.split(',')
        elif not train_split and config.dataset_validation_files:
            dataset_files = config.dataset_validation_files.split(',')
        elif config.dataset:
            dataset_dir = os.path.join(config.data_dir, config.dataset)
            prefix = config.dataset_train_file_prefix if train_split else config.dataset_validation_file_prefix
            for dataset_file in glob.glob(os.path.join(dataset_dir, "*.*")):
                if self.is_file_type_supported(dataset_file) and os.path.basename(dataset_file).startswith(prefix):
                    dataset_files.append(dataset_file)
            self.logger.info(f"Found {len(dataset_files)} files in {dataset_dir} with prefix '{prefix}'")
        if dataset_files:
            return sorted(dataset_files)
        elif train_split:
            raise Exception('Training dataset files not found!')
        else:
            return []
    
    def is_file_type_supported(self, dataset_file):
        return dataset_file.endswith('.bin') or dataset_file.endswith('.pt') or dataset_file.endswith('.alm')
    
    def load_next_dataset(self):
        self.data = None
        gc.collect()
        for ds_file in self.dataset_files:
            if ds_file not in self.processed_files:
                if self.load_dataset_file(ds_file):
                    return True
        return False
                
    def load_dataset_file(self, load_dataset_file):
        self.processed_files.append(load_dataset_file)
        new_data = None
        if load_dataset_file.endswith('.bin'):
            assert self.training_type == 'pre', 'NumPy format is supported only for pre-training'
            step_size = self.world_size * self.sample_size
            new_data = torch.from_numpy(np.fromfile(load_dataset_file, dtype=np.uint16).astype(np.int16))
            if step_size > len(new_data):
                self.logger.warning(
                    f"Dataset file {load_dataset_file} does not have enough data and will be ignored. "
                    f"Expected at least {step_size} tokens but found only {len(new_data)}"
                )
                return False
            new_data = self.align_data_to_step_size(new_data, step_size)
            new_data = self.transform_continuous_data_to_samples(new_data)
            new_data = self.limit_samples_to_rank(new_data)
        elif load_dataset_file.endswith('.pt'):
            assert self.training_type != 'dpo', 'DPO training only supports the ALM format'
            new_data = torch.load(load_dataset_file, map_location='cpu')
            if isinstance(new_data, torch.Tensor):
                step_size = self.world_size * self.sample_size
                if step_size > len(new_data):
                    self.logger.warning(
                        f"Dataset file {load_dataset_file} does not have enough data and will be ignored. "
                        f"Expected at least {step_size} tokens but found only {len(new_data)}"
                    )
                    return False
                new_data = self.align_data_to_step_size(new_data, step_size)
                new_data = self.transform_continuous_data_to_samples(new_data)
                new_data = self.limit_samples_to_rank(new_data)
            else:
                new_data = self.align_and_limit_to_rank(new_data, load_dataset_file)
                if new_data:
                    self.pad_or_truncate_to_block_size(new_data)
        elif load_dataset_file.endswith('.alm'):
            new_data = joblib.load(load_dataset_file)
            new_data = self.align_and_limit_to_rank(new_data, load_dataset_file)
        
        if new_data:
            self.data = new_data
            self.data_in_alm_format = load_dataset_file.endswith('.alm')
            self.logger.info(f"New dataset file {load_dataset_file} loaded. Processed files: {len(self.processed_files)}")
            gc.collect()
            return True
        else:
            return False
        
    def align_and_limit_to_rank(self, new_data, load_dataset_file):
        if isinstance(new_data, list):
            if self.world_size > len(new_data):
                self.logger.warning(
                    f"Dataset file {load_dataset_file} does not have enough data and will be ignored. "
                    f"Expected at least {self.world_size} samples but found only {len(new_data)}"
                )
                return None
            new_data = self.align_data_to_step_size(new_data, self.world_size)
            new_data = self.limit_samples_to_rank(new_data)
        else:
            self.logger.info(f"Unsupported format of {load_dataset_file}!")
            new_data = None
        return new_data
    
    def align_data_to_step_size(self, data, step_size):
        target_length = ((len(data) + step_size - 1) // step_size) * step_size
        padding_length = target_length - len(data)
        if padding_length > 0:
            pre_size = len(data)
            if isinstance(data, list):
                data.extend(data[:padding_length])
            else:
                data = torch.concat((data, data[:padding_length]))
            self.logger.info(f"Data aligned. Pre-alignment size: {pre_size}, "
                             f"post-alignment size: {len(data)}, "
                             f"padding added: {padding_length}")
        return data
        
    def transform_continuous_data_to_samples(self, data):
        return [data[i:i + self.sample_size] for i in range(0, len(data), self.sample_size)]
        
    def pad_or_truncate_to_block_size(self, data):
        """
        Adds padding to instructions to maintain a consistent input shape, avoiding recompilations.
        This method ensures all instructions have a uniform length matching the block size.
        By doing so, it prevents the need for frequent recompilations that occur due to
        dynamic input shapes, enhancing computational efficiency and stability.
        """
        for idx in range(len(data)):
            if isinstance(data[idx], dict):
                if 'input_ids' not in data[idx]:
                    raise Exception(f"'input_ids' field not found in sample! Available keys: {', '.join(data[idx].keys())}")
                elif isinstance(data[idx]['input_ids'], np.ndarray):
                    data[idx]['input_ids'] = torch.from_numpy(data[idx]['input_ids'])
                if 'target_ids' not in data[idx]:
                    data[idx]['target_ids'] = data[idx]['input_ids'][1:]
                elif isinstance(data[idx]['target_ids'], np.ndarray):
                    data[idx]['target_ids'] = torch.from_numpy(data[idx]['target_ids'])
                
                if self.weighted_loss:
                    if 'target_weights' not in data[idx]:
                        data[idx]['target_weights'] = torch.where(data[idx]['target_ids'] == self.ignore_index, 0, 1)
                    elif isinstance(data[idx]['target_weights'], np.ndarray):
                        data[idx]['target_weights'] = torch.from_numpy(data[idx]['target_weights'])
                elif 'target_weights' in data[idx]:
                    del data[idx]['target_weights']
                    
                if len(data[idx]['input_ids']) >= self.sample_size: # block_size = sample_size - 1
                    data[idx]['input_ids'] = data[idx]['input_ids'][:self.sample_size-1]
                elif self.pad_token_id >= 0 and len(data[idx]['input_ids']) < self.sample_size-1:
                    padding = self.sample_size - 1 - len(data[idx]['input_ids'])
                    data[idx]['input_ids'] = torch.cat([data[idx]['input_ids'], torch.full((padding,), self.ignore_index)], dim=0)
                
                if len(data[idx]['target_ids']) >= self.sample_size:
                    data[idx]['target_ids'] = data[idx]['target_ids'][:self.sample_size-1]
                elif self.pad_token_id >= 0 and len(data[idx]['target_ids']) < self.sample_size-1:
                    padding = self.sample_size - 1 - len(data[idx]['target_ids'])
                    data[idx]['target_ids'] = torch.cat([data[idx]['target_ids'], torch.full((padding,), self.ignore_index)], dim=0)
                
                if self.weighted_loss:
                    if len(data[idx]['target_weights']) >= self.sample_size:
                        data[idx]['target_weights'] = data[idx]['target_weights'][:self.sample_size-1]
                    elif self.pad_token_id >= 0 and len(data[idx]['target_weights']) < self.sample_size-1:
                        padding = self.sample_size - 1 - len(data[idx]['target_weights'])
                        data[idx]['target_weights'] = torch.cat([data[idx]['target_weights'], torch.full((padding,), 0)], dim=0)
                
                assert len(data[idx]['input_ids']) == len(data[idx]['target_ids'])
                if self.weighted_loss:
                    assert len(data[idx]['input_ids']) == len(data[idx]['target_weights'])
            else:
                if len(data[idx]) > self.sample_size:
                    data[idx] = data[idx][:self.sample_size]
                if self.pad_token_id >= 0:
                    if len(data[idx]) < self.sample_size:
                        padding = self.sample_size - len(data[idx])
                        data[idx] = torch.cat([data[idx], torch.full((padding,), self.ignore_index)], dim=0)
                    input_ids = data[idx][:-1]
                    target_ids = data[idx][1:]
                    target_weights = torch.where(target_ids == self.ignore_index, 0, 1)
                    input_ids = input_ids.masked_fill(input_ids == self.ignore_index, self.pad_token_id)
                    data[idx] = {'input_ids': input_ids, 'target_ids': target_ids, 'target_weights': target_weights}
        
    def limit_samples_to_rank(self, samples):
        return list(s for s in samples[self.rank::self.world_size]) if self.world_size > 1 else samples
        
    def has_data(self):
        return self.data and len(self.data) > 0
    
    def prepare_alm_dpo_sample(self, sample):
        result = {
            'chosen_input_ids': torch.from_numpy(sample['chosen_input_ids']),
            'chosen_target_ids': torch.from_numpy(sample['chosen_target_ids']),
            'rejected_input_ids': torch.from_numpy(sample['rejected_input_ids']),
            'rejected_target_ids': torch.from_numpy(sample['rejected_target_ids'])
        }
        if "reference_chosen_logps" in sample and "reference_rejected_logps" in sample:
            result["reference_chosen_logps"] = torch.tensor(sample['reference_chosen_logps'])
            result["reference_rejected_logps"] = torch.tensor(sample['reference_rejected_logps'])
        
        if self.pad_token_id >= 0:
            if len(result['chosen_input_ids']) < self.block_size:
                result['chosen_input_ids'] = F.pad(result['chosen_input_ids'], (0, self.block_size - len(result['chosen_input_ids'])), value=self.pad_token_id)
            if len(result['chosen_target_ids']) < self.block_size:
                result['chosen_target_ids'] = F.pad(result['chosen_target_ids'], (0, self.block_size - len(result['chosen_target_ids'])), value=self.ignore_index)
            if len(result['rejected_input_ids']) < self.block_size:
                result['rejected_input_ids'] = F.pad(result['rejected_input_ids'], (0, self.block_size - len(result['rejected_input_ids'])), value=self.pad_token_id)
            if len(result['rejected_target_ids']) < self.block_size:
                result['rejected_target_ids'] = F.pad(result['rejected_target_ids'], (0, self.block_size - len(result['rejected_target_ids'])), value=self.ignore_index)
        
        return result
    
    def prepare_alm_sample(self, sample):
        """
        Assumes input sample contains at least 'input_ids' and 'target_ids' fields. 
        When the weighted loss is active, 'target_weights' field is required.
        When samples are packed, it is assumed that a list of sequence lengths will be available
        in the "seq_lens" field. This information will be used to create the attention mask.
        If pad_token_id is set in the configuration, it is assumed that the sample list
        did not have padding and samples are of length up to block_size.
        """
        if self.training_type == 'dpo':
            return self.prepare_alm_dpo_sample(sample)
        
        result = {
            'input_ids': torch.from_numpy(sample['input_ids']),
            'target_ids': torch.from_numpy(sample['target_ids'])
        }
        if self.weighted_loss:
            if 'target_weights' in sample:
                result['target_weights'] = torch.from_numpy(sample['target_weights'])
            else:
                result['target_weights'] = torch.where(result['target_ids'] == self.ignore_index, 0, 1)
        
        if self.pad_token_id >= 0:
            if len(result['input_ids']) < self.block_size:
                result['input_ids'] = F.pad(result['input_ids'], (0, self.block_size - len(result['input_ids'])), value=self.pad_token_id)
            if len(result['target_ids']) < self.block_size:
                result['target_ids'] = F.pad(result['target_ids'], (0, self.block_size - len(result['target_ids'])), value=self.ignore_index)
            if 'target_weights' in result and len(result['target_weights']) < self.block_size:
                result['target_weights'] = F.pad(result['target_weights'], (0, self.block_size - len(result['target_weights'])), value=0)
        
        if "seq_lens" in sample:
            total_seq_len = 0
            block_attn_masks = []
            sample_input_pos = []
            for seq_len in sample["seq_lens"]:
                sample_input_pos.extend(list(range(seq_len)))
                total_seq_len += seq_len
                
                # append lower triangular matrix for causal mask
                block_attn_masks.append(torch.tril(torch.ones(seq_len, seq_len, dtype=torch.bool)))
                
            if total_seq_len < len(result['input_ids']):
                new_pos = sample_input_pos[-1] + 1
                num_pad = len(result['input_ids']) - total_seq_len
                sample_input_pos.extend(list(range(new_pos, new_pos + num_pad)))
                block_attn_masks.append(torch.eye(num_pad, num_pad, dtype=torch.bool))
            result['input_pos'] = torch.tensor(sample_input_pos)
            result['attn_mask'] = torch.block_diag(*block_attn_masks)
        return result
    
    def __len__(self):
        """ Size of currently loaded dataset file """
        return len(self.data) if self.data else 0
        
    def __getitem__(self, idx):
        result = None
        if isinstance(idx, slice):
            result = self.data[idx]
            if self.data_in_alm_format:
                result = list(self.prepare_alm_sample(s) for s in result)
        elif idx < self.__len__():
            result = self.data[idx]
            if self.data_in_alm_format:
                result = self.prepare_alm_sample(result)
        return result
        

class AllamoDataLoader:

    def __init__(self, config: AllamoConfiguration, rank=None, world_size=None):
        self.logger = logging.getLogger('AllamoDataLoader')
        self.config = config
        self.epoch = 0
        self.rank = rank if rank is not None else 0
        self.world_size = world_size if world_size is not None else 1
        self.pin_memory = True
        
        if config.batch_size_schedule: 
            self.config.batch_size_max = config.batch_size
            self.batch_size = config.batch_size_initial
        else:
            self.batch_size = config.batch_size
        
        if self.config.dataset_seq_train:
            self.dataset_offset = config.dataset_seq_train_start if config.dataset_seq_train_start is not None else 0
        else:
            self.dataset_offset = 0
        self.logger.info(f"Training dataset offset set to {self.dataset_offset:,}")
        
        self.load_datasets()
        self.buffer = None
        self.buffer_lock = threading.Lock()
        self.buffer_thread = None
            
    def load_datasets(self):
        timer = time.time()
        self.train_dataset = AllamoDataset(self.config, True, self.rank, self.world_size)
        self.splits = ['train']
        self.logger.info(f"Training dataset created with files: {','.join(self.train_dataset.dataset_files)}")
        self.logger.info(f"Training samples loaded: {(len(self.train_dataset)*self.world_size):,}")
        
        self.val_dataset = AllamoDataset(self.config, False, self.rank, self.world_size)
        if self.val_dataset.has_data():
            self.splits.append('val')
            self.logger.info(f"Validation dataset created with files: {','.join(self.val_dataset.dataset_files)}")
            self.logger.info(f"Validation samples loaded: {(len(self.val_dataset)*self.world_size):,}")
        else:
            self.val_dataset = None
            self.logger.info(f"Validation dataset is missing. Testing only on the training dataset")
        dt = time.time() - timer
        self.logger.info(f"Datasets loaded in {dt:.2f} secs")
        
    def get_splits(self):
        return self.splits
    
    def prepare_dpo_samples(self, samples):
        chosen_input_ids = torch.stack([sample['chosen_input_ids'] for sample in samples]).to(torch.int64)
        chosen_target_ids = torch.stack([sample['chosen_target_ids'] for sample in samples]).to(torch.int64)
        rejected_input_ids = torch.stack([sample['rejected_input_ids'] for sample in samples]).to(torch.int64)
        rejected_target_ids = torch.stack([sample['rejected_target_ids'] for sample in samples]).to(torch.int64)
        reference_chosen_logps = torch.stack([sample['reference_chosen_logps'] for sample in samples]).to(torch.float32) if 'reference_chosen_logps' in samples[0] else None
        reference_rejected_logps = torch.stack([sample['reference_rejected_logps'] for sample in samples]).to(torch.float32) if 'reference_rejected_logps' in samples[0] else None
        
        if 'cuda' in self.config.device and self.pin_memory:
            chosen_input_ids = chosen_input_ids.pin_memory().to(self.config.device, non_blocking=True)
            chosen_target_ids = chosen_target_ids.pin_memory().to(self.config.device, non_blocking=True)
            rejected_input_ids = rejected_input_ids.pin_memory().to(self.config.device, non_blocking=True)
            rejected_target_ids = rejected_target_ids.pin_memory().to(self.config.device, non_blocking=True)
            if reference_chosen_logps is not None:
                reference_chosen_logps = reference_chosen_logps.pin_memory().to(self.config.device, non_blocking=True)
            if reference_rejected_logps is not None:
                reference_rejected_logps = reference_rejected_logps.pin_memory().to(self.config.device, non_blocking=True)
        else:
            chosen_input_ids = chosen_input_ids.to(self.config.device)
            chosen_target_ids = chosen_target_ids.to(self.config.device)
            rejected_input_ids = rejected_input_ids.to(self.config.device)
            rejected_target_ids = rejected_target_ids.to(self.config.device)
            if reference_chosen_logps is not None:
                reference_chosen_logps = reference_chosen_logps.to(self.config.device)
            if reference_rejected_logps is not None:
                reference_rejected_logps = reference_rejected_logps.to(self.config.device)
        return {
            "chosen_input_ids": chosen_input_ids,
            "chosen_target_ids": chosen_target_ids,
            "rejected_input_ids": rejected_input_ids,
            "rejected_target_ids": rejected_target_ids,
            "reference_chosen_logps": reference_chosen_logps,
            "reference_rejected_logps": reference_rejected_logps
        }
    
    def prepare_samples(self, samples):
        if self.config.training_type == 'dpo':
            return self.prepare_dpo_samples(samples)
        
        if isinstance(samples[0], dict):
            input_ids = torch.stack([sample['input_ids'] for sample in samples]).to(torch.int64)
            target_ids = torch.stack([sample['target_ids'] for sample in samples]).to(torch.int64)
            target_weights = torch.stack([sample['target_weights'] for sample in samples]).to(torch.float32) if 'target_weights' in samples[0] else None
            attn_mask = torch.stack([sample['attn_mask'] for sample in samples]) if 'attn_mask' in samples[0] else None
            input_pos = torch.stack([sample['input_pos'] for sample in samples]) if 'input_pos' in samples[0] else None
        else:
            input_ids = torch.stack([sample[:-1] for sample in samples]).to(torch.int64)
            target_ids = torch.stack([sample[1:] for sample in samples]).to(torch.int64)
            target_weights = None
            attn_mask = None
            input_pos = None
        
        if 'cuda' in self.config.device and self.pin_memory:
            input_ids = input_ids.pin_memory().to(self.config.device, non_blocking=True)
            target_ids = target_ids.pin_memory().to(self.config.device, non_blocking=True)
            if target_weights is not None:
                target_weights = target_weights.pin_memory().to(self.config.device, non_blocking=True)
            if attn_mask is not None and input_pos is not None:
                attn_mask = attn_mask.pin_memory().to(self.config.device, non_blocking=True)
                input_pos = input_pos.pin_memory().to(self.config.device, non_blocking=True)
        else:
            input_ids = input_ids.to(self.config.device)
            target_ids = target_ids.to(self.config.device)
            if target_weights is not None:
                target_weights = target_weights.to(self.config.device)
            if attn_mask is not None and input_pos is not None:
                attn_mask = attn_mask.to(self.config.device)
                input_pos = input_pos.to(self.config.device)
        return {
            "input_ids": input_ids,
            "target_ids": target_ids,
            "target_weights": target_weights,
            "attn_mask": attn_mask,
            "input_pos": input_pos
        }
        
    def update_buffer(self, dataset):
        with self.buffer_lock:
            self.buffer = {
                "batch": self.prepare_samples(dataset[self.dataset_offset:self.dataset_offset+self.batch_size]),
                "offset": self.dataset_offset + self.batch_size
            }
    
    def reload_buffer(self, dataset):
        self.buffer = None
        if self.dataset_offset + self.batch_size <= len(dataset):
            self.buffer_thread = threading.Thread(target=self.update_buffer, args=(dataset,))
            self.buffer_thread.start()
        else:
            self.buffer_thread = None
            
    def get_batch_from_buffer(self, dataset):
        with self.buffer_lock:
            batch = self.buffer["batch"]
            self.dataset_offset = self.buffer["offset"]
        assert self.buffer_thread is None or not self.buffer_thread.is_alive()
        self.reload_buffer(dataset)
        return batch
        
    def get_batch(self, split='train', random_samples=False):
        if split == 'train' or self.val_dataset is None:
            dataset = self.train_dataset
        else:
            dataset = self.val_dataset
        
        if random_samples == False and split == 'train' and self.config.dataset_seq_train:
            if self.config.dataset_buffer and self.buffer is not None:
                return self.get_batch_from_buffer(dataset)
            elif self.dataset_offset + self.batch_size <= len(dataset):
                samples = dataset[self.dataset_offset:self.dataset_offset+self.batch_size]
                self.dataset_offset += self.batch_size
            else:
                samples = []
                for _ in range(self.batch_size):
                    if self.dataset_offset >= len(dataset):
                        self.reload_dataset(dataset)
                    samples.append(dataset[self.dataset_offset])
                    self.dataset_offset += 1
            self.reload_buffer(dataset)
        else:
            idx_batch = torch.randint(len(dataset), (self.batch_size,))
            samples = [dataset[i] for i in idx_batch]
            
        return self.prepare_samples(samples)
    
    def reload_dataset(self, dataset):
        if len(dataset.dataset_files) > 1:
            if dataset.load_next_dataset():
                # Epoch is not finished, we've just loaded next dataset file
                self.dataset_offset = 0
                return
            else:
                dataset.processed_files.clear()
                assert dataset.load_next_dataset(), 'Something very bad has happend and we are unable to reload dataset'
        self.dataset_offset = 0
        self.epoch += 1
        self.logger.info(f"Epoch {self.epoch} finished")
        
    def update_batch_size(self, iter_num):
        if self.config.batch_size_schedule and self.batch_size < self.config.batch_size_max:
            self.batch_size = min(self.batch_size + 1, self.config.batch_size_max) if iter_num % (self.config.batch_size_max_iter/100) == 0 else self.batch_size 
        return self.batch_size
        
