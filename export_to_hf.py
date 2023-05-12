"""
Use this file to export ALLaMo weights to Huggingface LLaMA model.   
"""
import argparse
import datetime
import gc
import json
import os
import shutil
import torch
from transformers import LlamaConfig, LlamaForCausalLM


def timestamp():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    

def compute_intermediate_size(config):
    return config.multiple_of * ((int(config.n_embd * 8 / 3) + config.multiple_of - 1) // config.multiple_of)


def read_json(path):
    with open(path, "r") as f:
        return json.load(f)


def write_json(text, path):
    with open(path, "w") as f:
        json.dump(text, f)

def write_model(checkpoint_path, hf_model_path):
    os.makedirs(hf_model_path, exist_ok=True)
    tmp_model_path = os.path.join(hf_model_path, "tmp")
    os.makedirs(tmp_model_path, exist_ok=True)
    
    checkpoint_path = checkpoint_path if checkpoint_path.endswith('.pt') else os.path.join(checkpoint_path, 'ckpt.pt')
    print(f"{timestamp()} - loading checkpoint from {checkpoint_path}...")
    checkpoint = torch.load(checkpoint_path, map_location='cpu')
    allamo_transformer_config = checkpoint['model_args']

    n_layers = allamo_transformer_config.n_layer
    n_heads = allamo_transformer_config.n_head
    dim = allamo_transformer_config.n_embd
    dims_per_head = allamo_transformer_config.head_size

    print(f"{timestamp()} - converting all parameters from the checkpoint model")
    loaded = checkpoint['model']
    unwanted_prefix = '_orig_mod.'
    for k,v in list(loaded.items()):
        if k.startswith(unwanted_prefix):
            loaded[k[len(unwanted_prefix):]] = loaded.pop(k)
            
    param_count = 0
    index_dict = {"weight_map": {}}
    for layer_i in range(n_layers):
        print(f"{timestamp()} - converting weights in layer {layer_i}")
        filename = f"pytorch_model-{layer_i + 1}-of-{n_layers + 1}.bin"
        state_dict = {
            f"model.layers.{layer_i}.self_attn.q_proj.weight": loaded[f"layers.{layer_i}.attention.q_proj.weight"],
            f"model.layers.{layer_i}.self_attn.k_proj.weight": loaded[f"layers.{layer_i}.attention.k_proj.weight"],
            f"model.layers.{layer_i}.self_attn.v_proj.weight": loaded[f"layers.{layer_i}.attention.v_proj.weight"],
            f"model.layers.{layer_i}.self_attn.o_proj.weight": loaded[f"layers.{layer_i}.attention.c_proj.weight"],
            f"model.layers.{layer_i}.self_attn.rotary_emb.inv_freq": loaded[f"layers.{layer_i}.attention.rotary_emb.inv_freq"],
            f"model.layers.{layer_i}.mlp.gate_proj.weight": loaded[f"layers.{layer_i}.feed_forward.gate_proj.weight"],
            f"model.layers.{layer_i}.mlp.down_proj.weight": loaded[f"layers.{layer_i}.feed_forward.down_proj.weight"],
            f"model.layers.{layer_i}.mlp.up_proj.weight": loaded[f"layers.{layer_i}.feed_forward.up_proj.weight"],
            f"model.layers.{layer_i}.input_layernorm.weight": loaded[f"layers.{layer_i}.attention_norm.weight"],
            f"model.layers.{layer_i}.post_attention_layernorm.weight": loaded[f"layers.{layer_i}.ffn_norm.weight"]
        }
        for k, v in state_dict.items():
            index_dict["weight_map"][k] = filename
            param_count += v.numel()
        torch.save(state_dict, os.path.join(tmp_model_path, filename))

    filename = f"pytorch_model-{n_layers + 1}-of-{n_layers + 1}.bin"
    state_dict = {
        "model.embed_tokens.weight": loaded["tok_embeddings.weight"],
        "model.norm.weight": loaded["norm.weight"],
        "lm_head.weight": loaded["lm_head.weight"],
    }

    for k, v in state_dict.items():
        index_dict["weight_map"][k] = filename
        param_count += v.numel()
    torch.save(state_dict, os.path.join(tmp_model_path, filename))
    print(f"{timestamp()} - {param_count} params converted to HF LLaMA model")

    # Write configs
    index_dict["metadata"] = {"total_size": param_count * 2}
    write_json(index_dict, os.path.join(tmp_model_path, "pytorch_model.bin.index.json"))

    config = LlamaConfig(
        vocab_size=allamo_transformer_config.vocab_size,
        hidden_size=dim,
        intermediate_size=compute_intermediate_size(allamo_transformer_config),
        num_attention_heads=n_heads,
        num_hidden_layers=n_layers,
        rms_norm_eps=allamo_transformer_config.norm_eps,
    )
    config.save_pretrained(tmp_model_path)
    print(f"{timestamp()} - configuration for the HF LLaMA model saved")

    # Make space so we can load the model properly now.
    del state_dict
    del loaded
    gc.collect()

    print(f"{timestamp()} - loading the checkpoint in a LLaMA model.")
    model = LlamaForCausalLM.from_pretrained(tmp_model_path, torch_dtype=torch.float16, low_cpu_mem_usage=True)
    # Avoid saving this as part of the config.
    del model.config._name_or_path

    print(f"{timestamp()} - saving in the Transformers format.")
    model.save_pretrained(hf_model_path)
    shutil.rmtree(tmp_model_path)
    print(f"{timestamp()} - conversion completed!")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input_dir",
        help="Location of ALLaMo weights, which contains a checkpoint file",
    )
    parser.add_argument(
        "--output_dir",
        help="Location to write HF model",
    )
    args = parser.parse_args()
    write_model(
        checkpoint_path=args.input_dir,
        hf_model_path=args.output_dir,
    )


if __name__ == "__main__":
    main()