from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import torch
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from torch.utils.data import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    DataCollatorForSeq2Seq,
    EarlyStoppingCallback,
    Trainer,
    TrainingArguments,
)


ROOT = Path(__file__).resolve().parents[1]
ROLE_IDS = (
    "functional_reviewer",
    "refactoring_reviewer",
    "documentation_reviewer",
    "requirements_reviewer",
    "test_reviewer",
)


class ReviewDataset(Dataset):
    def __init__(
        self,
        path: Path,
        tokenizer: Any,
        max_length: int,
        max_samples: int,
        role_id: str,
    ) -> None:
        if not path.exists():
            raise FileNotFoundError(f"Prepared training file not found: {path}")
        self.rows: list[dict[str, list[int]]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("role_id") != role_id:
                    continue
                messages = row["messages"]
                prompt_ids = tokenizer.apply_chat_template(
                    messages[:-1], tokenize=True, add_generation_prompt=True, enable_thinking=False
                )
                full_ids = tokenizer.apply_chat_template(
                    messages, tokenize=True, enable_thinking=False
                )
                if len(prompt_ids) >= max_length or len(full_ids) <= len(prompt_ids):
                    continue
                completion_ids = full_ids[len(prompt_ids) :]
                room = max_length - len(prompt_ids)
                # Keep structured JSON targets intact; truncating a completion
                # can silently turn an otherwise useful example into invalid JSON.
                if len(completion_ids) > room:
                    continue
                input_ids = prompt_ids + completion_ids
                labels = [-100] * len(prompt_ids) + completion_ids
                self.rows.append({"input_ids": input_ids, "labels": labels})
                if max_samples > 0 and len(self.rows) >= max_samples:
                    break
        if not self.rows:
            raise RuntimeError(f"No usable samples in {path}")

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> dict[str, list[int]]:
        return self.rows[index]


def main() -> None:
    parser = argparse.ArgumentParser(description="Train one role-specific PRGuard QLoRA adapter")
    parser.add_argument("--model-path", default=str(ROOT / "models" / "base" / "Qwen3-1.7B"))
    parser.add_argument("--train-file", default=str(ROOT / "data" / "processed" / "train.jsonl"))
    parser.add_argument("--eval-file", default=str(ROOT / "data" / "processed" / "validation.jsonl"))
    parser.add_argument("--role-id", required=True, choices=ROLE_IDS)
    parser.add_argument("--output-dir")
    parser.add_argument("--max-steps", type=int, default=0, help="Positive value overrides --epochs.")
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--evals-per-epoch", type=int, default=4)
    parser.add_argument("--early-stopping-patience", type=int, default=0)
    parser.add_argument("--max-samples", type=int, default=0)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--learning-rate", type=float, default=5e-5)
    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else ROOT / "outputs" / "adapters" / "by-role" / args.role_id
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(
            f"Refusing to overwrite non-empty adapter directory: {output_dir}. "
            "Choose a new output path or move the existing result first."
        )

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available in this Python environment; refusing CPU fallback.")
    model_path = Path(args.model_path)
    if not model_path.is_dir():
        raise FileNotFoundError(f"Local base model directory not found: {model_path}")

    tokenizer = AutoTokenizer.from_pretrained(str(model_path), local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.float16,
    )
    model = AutoModelForCausalLM.from_pretrained(
        str(model_path),
        quantization_config=quantization,
        device_map="auto",
        dtype=torch.float16,
        low_cpu_mem_usage=True,
        local_files_only=True,
    )
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model = get_peft_model(
        model,
        LoraConfig(
            r=8,
            lora_alpha=16,
            lora_dropout=0.05,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
            task_type="CAUSAL_LM",
            bias="none",
        ),
    )
    model.print_trainable_parameters()

    train_data = ReviewDataset(
        Path(args.train_file), tokenizer, args.max_length, args.max_samples, args.role_id
    )
    eval_data = None
    eval_path = Path(args.eval_file)
    if eval_path.exists() and eval_path.stat().st_size > 0:
        eval_data = ReviewDataset(
            eval_path, tokenizer, args.max_length, max_samples=0, role_id=args.role_id
        )
    if len(train_data) < 20:
        raise RuntimeError(f"Only {len(train_data)} usable training samples for {args.role_id}; refusing to train.")
    if eval_data is not None and len(eval_data) == 0:
        raise RuntimeError(f"No usable validation samples for {args.role_id} at max_length={args.max_length}.")
    if args.evals_per_epoch < 1:
        raise ValueError("--evals-per-epoch must be at least 1.")
    if args.max_steps <= 0 and args.epochs <= 0:
        raise ValueError("Provide a positive --max-steps or --epochs value.")

    print(f"role_id={args.role_id} train_samples={len(train_data)} eval_samples={len(eval_data) if eval_data else 0}")
    effective_batch_size = 8  # per-device batch 1 * gradient accumulation 8 * one GPU
    steps_per_epoch = math.ceil(len(train_data) / effective_batch_size)
    if args.max_steps > 0:
        planned_steps = args.max_steps
        eval_steps = max(1, planned_steps // args.evals_per_epoch)
    else:
        eval_steps = max(1, math.ceil(steps_per_epoch / args.evals_per_epoch))
        planned_steps = max(1, math.ceil(steps_per_epoch * args.epochs))
        # Align the final optimizer update with an evaluation/checkpoint boundary.
        planned_steps = max(eval_steps, math.ceil(planned_steps / eval_steps) * eval_steps)
    planned_epochs = planned_steps / steps_per_epoch
    print(
        f"planned_steps={planned_steps} steps_per_epoch={steps_per_epoch} "
        f"planned_epochs={planned_epochs:.3f} eval_save_interval={eval_steps}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    training_args = TrainingArguments(
        output_dir=str(output_dir),
        max_steps=planned_steps,
        per_device_train_batch_size=1,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=8,
        gradient_checkpointing=True,
        learning_rate=args.learning_rate,
        fp16=True,
        bf16=False,
        logging_steps=5,
        save_strategy="steps",
        save_steps=eval_steps,
        eval_strategy="steps" if eval_data is not None else "no",
        eval_steps=eval_steps,
        save_total_limit=2,
        load_best_model_at_end=eval_data is not None,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        warmup_ratio=0.05,
        weight_decay=0.01,
        report_to="none",
        dataloader_num_workers=0,
        remove_unused_columns=False,
        optim="adamw_torch",
    )
    collator = DataCollatorForSeq2Seq(
        tokenizer=tokenizer,
        padding=True,
        label_pad_token_id=-100,
        pad_to_multiple_of=8,
        return_tensors="pt",
    )
    callbacks = []
    if eval_data is not None and args.early_stopping_patience > 0:
        callbacks.append(
            EarlyStoppingCallback(early_stopping_patience=args.early_stopping_patience)
        )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_data,
        eval_dataset=eval_data,
        data_collator=collator,
        processing_class=tokenizer,
        callbacks=callbacks,
    )
    train_result = trainer.train()
    trainer.save_model(str(output_dir))
    eval_metrics = trainer.evaluate() if eval_data is not None else {}
    summary = {
        "role_id": args.role_id,
        "base_model": str(model_path),
        "train_file": str(Path(args.train_file).resolve()),
        "eval_file": str(Path(args.eval_file).resolve()),
        "train_samples_usable": len(train_data),
        "validation_samples_usable": len(eval_data) if eval_data else 0,
        "max_steps": planned_steps,
        "steps_per_epoch": steps_per_epoch,
        "planned_epochs": planned_epochs,
        "eval_save_interval_steps": eval_steps,
        "best_checkpoint": trainer.state.best_model_checkpoint,
        "best_validation_loss": trainer.state.best_metric,
        "evaluation_history": [
            {key: value for key, value in entry.items() if key in {"step", "epoch", "eval_loss"}}
            for entry in trainer.state.log_history
            if "eval_loss" in entry
        ],
        "max_length": args.max_length,
        "learning_rate": args.learning_rate,
        "train_metrics": train_result.metrics,
        "validation_metrics": eval_metrics,
    }
    (output_dir / "training_metrics.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"role_id={args.role_id} final_validation_metrics={eval_metrics}")
    print(f"Adapter saved to: {output_dir}")


if __name__ == "__main__":
    main()

