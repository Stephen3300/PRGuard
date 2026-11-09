from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import torch


ROLE_IDS = (
    "functional_reviewer",
    "refactoring_reviewer",
    "documentation_reviewer",
    "requirements_reviewer",
    "test_reviewer",
)


def load_local_model(
    base_model: str,
    adapter_path: str,
    *,
    legacy_adapter_root: str | None = None,
    legacy_role_ids: tuple[str, ...] = (),
) -> tuple[Any, Any]:
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    if not torch.cuda.is_available():
        raise RuntimeError(
            "当前 PyTorch 未启用 CUDA。请在 PRGuard 的虚拟环境安装 CUDA 版 PyTorch 后重试。"
        )
    if not Path(base_model).exists():
        raise FileNotFoundError(f"基础模型目录不存在：{base_model}")
    if not Path(adapter_path).exists():
        raise FileNotFoundError(f"LoRA Adapter 目录不存在：{adapter_path}")

    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.float16,
    )
    tokenizer = AutoTokenizer.from_pretrained(base_model, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        base_model,
        quantization_config=quantization,
        device_map="auto",
        dtype=torch.float16,
        local_files_only=True,
    )
    adapter_root = Path(adapter_path)
    unknown_roles = set(legacy_role_ids) - set(ROLE_IDS)
    if unknown_roles:
        raise ValueError(f"Unknown legacy role IDs: {sorted(unknown_roles)}")
    if legacy_role_ids and not legacy_adapter_root:
        raise ValueError("legacy_adapter_root is required when legacy_role_ids are configured")
    fallback_root = Path(legacy_adapter_root) if legacy_adapter_root else None
    legacy_roles = set(legacy_role_ids)
    role_adapter_paths = {
        role_id: (
            fallback_root / role_id
            if role_id in legacy_roles and fallback_root is not None
            else adapter_root / role_id
        )
        for role_id in ROLE_IDS
    }
    has_role_adapters = all((path / "adapter_config.json").is_file() for path in role_adapter_paths.values())
    if has_role_adapters:
        first_role = ROLE_IDS[0]
        model = PeftModel.from_pretrained(
            model,
            str(role_adapter_paths[first_role]),
            adapter_name=first_role,
            local_files_only=True,
        )
        for role_id in ROLE_IDS[1:]:
            model.load_adapter(
                str(role_adapter_paths[role_id]),
                adapter_name=role_id,
                local_files_only=True,
            )
        model.prguard_adapter_names = set(ROLE_IDS)
        model.prguard_adapter_sources = {
            role_id: str(path) for role_id, path in role_adapter_paths.items()
        }
        model.prguard_legacy_role_ids = legacy_roles
    else:
        if legacy_role_ids:
            missing = [
                f"{role_id}: {role_adapter_paths[role_id]}"
                for role_id in ROLE_IDS
                if not (role_adapter_paths[role_id] / "adapter_config.json").is_file()
            ]
            raise FileNotFoundError(
                "Mixed role adapter set is incomplete: " + "; ".join(missing)
            )
        model = PeftModel.from_pretrained(model, adapter_path, local_files_only=True)
        model.prguard_adapter_names = set()
    model.eval()
    return tokenizer, model


def generate_review(
    tokenizer: Any,
    model: Any,
    *,
    system_prompt: str,
    user_prompt: str,
    adapter_name: str | None = None,
    max_new_tokens: int = 180,
    deterministic: bool = False,
) -> str:
    available_adapters = getattr(model, "prguard_adapter_names", None)
    if adapter_name and available_adapters:
        if adapter_name not in available_adapters:
            raise KeyError(f"No role-specific Adapter loaded for {adapter_name}")
        model.set_adapter(adapter_name)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    input_ids = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        enable_thinking=False,
        return_tensors="pt",
    ).to(model.device)
    # Qwen uses the end-of-turn token as EOS; passing a bare tensor makes
    # Transformers unable to distinguish it from padding. This is a single
    # unpadded prompt, so explicitly mark every input token as visible.
    attention_mask = torch.ones_like(input_ids, device=model.device)
    with torch.inference_mode():
        generation_kwargs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "max_new_tokens": max_new_tokens,
            "pad_token_id": tokenizer.eos_token_id,
        }
        if deterministic:
            generation_kwargs["do_sample"] = False
        else:
            generation_kwargs.update(
                do_sample=True,
                temperature=0.7,
                top_p=0.8,
                top_k=20,
            )
        output = model.generate(
            **generation_kwargs,
        )
    return tokenizer.decode(output[0][input_ids.shape[-1] :], skip_special_tokens=True).strip()

