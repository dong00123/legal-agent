#!/usr/bin/env bash
# A100（40G/80G）单卡 LoRA 训练脚本
#
# 用法：
#   bash backend/finetune/llama_factory/train_a100.sh
#
# 前置：
#   1. 已装 LLaMA-Factory：pip install -e ".[torch,metrics]"
#   2. 数据已就位：cp <项目>/data/finetune/sft.jsonl data/sft.jsonl
#   3. dataset_info.json 已放进 LLaMA-Factory 的 data/ 目录

set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen2.5-7B-Instruct}"
DATASET_DIR="${DATASET_DIR:-data}"
CONFIG="${CONFIG:-backend/finetune/llama_factory/qwen2p5_7b_lora_sft.yaml}"
OUTPUT="${OUTPUT:-saves/qwen2p5-7b/lora/legal}"

echo "== 训练前自检 =="
# **样本数是第一道关。** 少于 20 条就开训，得到的 LoRA 只会记住那几条，
# 上线后表现为"问什么都往那几个答案上靠"。宁可不训，也不要训一个坏习惯。
if [ ! -f "${DATASET_DIR}/sft.jsonl" ]; then
  echo "!! 找不到 ${DATASET_DIR}/sft.jsonl —— 先跑：python -m backend.finetune --days 90"
  exit 1
fi
N=$(wc -l < "${DATASET_DIR}/sft.jsonl")
echo "样本行数：${N}"
if [ "${N}" -lt 20 ]; then
  echo "!! 样本少于 20 条，不建议开训。先攒数据，或降低 --days 以外的筛选判据（不推荐）。"
  exit 2
fi

echo "== 开始训练 =="
# FORCE_TORCHRUN=1 与 NNODES=1：单卡也走 torchrun，
# 这样换多卡时只需要改 NPROC_PER_NODE，不用改别的。
FORCE_TORCHRUN=1 NNODES=1 NPROC_PER_NODE=1 \
CUDA_VISIBLE_DEVICES=0 \
llamafactory-cli train "${CONFIG}" \
  --model_name_or_path "${MODEL}" \
  --dataset_dir "${DATASET_DIR}" \
  --output_dir "${OUTPUT}"

echo "== 合并 LoRA =="
# 训练完的 adapter 不能直接上 SGLang（它要的是完整权重）。
# 不合并就去部署，表现是"加载成功但回答和基座一模一样"——
# 看起来像微调没生效，其实是根本没把 adapter 装上去。
llamafactory-cli export \
  --model_name_or_path "${MODEL}" \
  --adapter_name_or_path "${OUTPUT}" \
  --template qwen \
  --finetuning_type lora \
  --export_dir "${OUTPUT}-merged" \
  --export_size 4 \
  --export_legacy_format False

echo "== 完成 =="
echo "合并后权重：${OUTPUT}-merged"
echo "**下一步是评测，不是上线**：用 backend/evals 对新旧模型各跑一份报告对比，"
echo "重点看「引用可采信率」有没有下降 —— 那一项掉了，其他再好也不能上线。"
