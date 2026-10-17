# LLaMA-Factory 训练配置

```
llama_factory/
  dataset_info.json              数据注册（alpaca 格式的字段映射）
  qwen2p5_7b_lora_sft.yaml       训练超参
  train_a100.sh                  A100 一键训练 + 合并
  README.md                      本文件
```

## 完整流程

```bash
# 1) 造数据（在项目根目录，本机就能跑）
python -m backend.finetune --days 90 --out data/finetune/sft.jsonl
#   先看体检报告的"淘汰原因"。产出为 0 时不要硬凑，按报告里最多的那条原因修。

# 2) 律师审核（**不可跳过**，见 ../REVIEW_CHECKLIST.md）

# 3) 把数据放进 LLaMA-Factory
cp data/finetune/sft.jsonl              <LLaMA-Factory>/data/sft.jsonl
cp backend/finetune/llama_factory/dataset_info.json <LLaMA-Factory>/data/dataset_info.json

# 4) 训练（GPU 机器）
bash backend/finetune/llama_factory/train_a100.sh
```

## 为什么是 LoRA 而不是全参

律所的数据量在几百到几千条这个量级。全参微调在这个量级上
把通用能力洗掉的风险明显大于收益 —— 表现是"法律问题答得好了，
但让它改个措辞、总结一段话就变笨"。LoRA 只调注意力投影层，
是可控的增量。

## 常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| 训练报"找不到数据集" | `dataset_info.json` 没放进 `dataset_dir` 指向的目录 | 它是**注册表**，必须和 `file_name` 在一起 |
| 加载成功但回答和基座一样 | 没合并 LoRA 就部署 | `train_a100.sh` 最后一步会合并，别跳过 |
| 显存 OOM | `cutoff_len` 太大 | 先降到 2048；仍不行就 `per_device_train_batch_size: 1` + 加大 `gradient_accumulation_steps` |
| 训完回答变啰嗦 | 训练集里混进了整篇法规 | 数据集构造已按 8000 字上限剔除，检查是不是自己改宽了 |
| 引用变少或编造 | 训练样本里有没有引用的 | 数据流水线已强制"无引用即剔除"，不要放宽这条 |

## 本机验证状态

- ✅ `python -m backend.finetune --days 120 --dry-run` 与正式产出均跑通（16 条样本）
- ✅ 产出的 JSONL 字段与 `dataset_info.json` 的映射对得上
- ❌ **训练本身未在本机执行**（无 GPU）。`train_a100.sh` 与 yaml 的字段口径
  来自 LLaMA-Factory 官方 schema，但首次在 A100 上跑时仍建议先用
  `max_samples: 8` 跑一个几十步的冒烟，确认数据能被正确加载再放开。
