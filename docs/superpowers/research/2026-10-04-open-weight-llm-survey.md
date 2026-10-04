# Open-weight LLM survey — 2026-10-04

Research input for the `kinoforge text` command design. Gathered by web search on
2026-10-04; sizes are measured safetensors bytes from the HF API. Re-verify before
shipping a config — model availability and leaderboard positions move monthly.

## Role A — best open-weight model per single-GPU VRAM tier

One family wins all three tiers: **Qwen3.8-27B** (released 2026-08-14, Apache 2.0,
dense 27.8B incl. vision encoder, 262K native ctx, **multimodal: image + video in**,
text out). HF card: GPQA Diamond 89.2, LiveCodeBench v6 90.3, SWE-bench Pro 61.7,
Terminal-Bench 2.1 73.0 (https://huggingface.co/Qwen/Qwen3.8-27B). Artificial Analysis
Intelligence Index v4.3.2 = 34, "#1 of 142 models in its class"
(https://artificialanalysis.ai/models/qwen3-8-27b). vLLM recipe needs vLLM >= 0.17 and
`--reasoning-parser qwen3` (https://recipes.vllm.ai/Qwen/Qwen3.8-27B).

| Tier | Pick (HF repo) | Format / on-disk | Notes | Runner-up |
|---|---|---|---|---|
| 24 GB (4090, A10) | `cyankiwi/Qwen3.8-27B-AWQ-INT4` (community) or `unsloth/Qwen3.8-27B-GGUF` UD-Q4_K_M | W4A16 AWQ ~16-17 GB; GGUF 16.5 GB | ~7 GB left for KV; cap ctx ~32K. No official Qwen int4; `nvidia/Qwen3.8-27B-NVFP4` is 24.6 GiB and Blackwell-only | `google/gemma-4-26B-A4B-it` MoE, ~15 GB at Q4 |
| 48 GB (A6000, L40S) | `Qwen/Qwen3.8-27B-FP8` (official) | FP8 block-128, 30.9 GB | Native W8A8 on Ada/Hopper; Ampere falls back to W8A16 via FP8-Marlin | `google/gemma-4-31B-it` at FP8 (~31 GB) |
| 80 GB (A100/H100) | `Qwen/Qwen3.8-27B` (bf16) | 55.6 GB | 24 GB KV headroom, full 262K ctx | `openai/gpt-oss-120b` MXFP4 65.2 GB, text-only, AA 12; `google/gemma-4-31B-it` bf16 62.6 GB, LMArena 1453 (#14 open, 2026-10-02), MMLU-Pro 85.2 / GPQA 84.3, AA 15 |

Nothing bigger fits one card: every open model above Gemma-4-31B on LMArena is a
295B-2.4T MoE (GLM-5.3-Flash 320B-A18B, MiMo-V2.6-Flash 309B-A15B, Hy3 295B-A21B,
Mistral Small 4 119B-A6.5B = 242 GB, Nemotron 3 Super 120B-A12B needs 2xH100 at FP8).
Nemotron-3-Nano-30B-A3B (60 GB bf16, text-only, MMLU-Pro 78.3) is the only other
single-GPU contender and trails Qwen3.8-27B. Phi-5 has no official card; Llama 5
(2026-06-30) ships only large sizes.

## Role B — ultrasmall smoke models

| Role | HF repo | Params / disk | Licence | Modality | Support |
|---|---|---|---|---|---|
| **Text-only pick** | `Qwen/Qwen3-0.6B` | 0.75B / 1.50 GB bf16 | Apache 2.0 | text only; 32K ctx | transformers >= 4.51, vLLM >= 0.8.5; `enable_thinking=False` for terse replies |
| text alt | `LiquidAI/LFM2-350M` | 0.35B / 0.71 GB | LFM Open License v1.0 | text only | vLLM text table |
| text alt | `google/gemma-3-270m-it` | 0.27B / 0.54 GB | Gemma (non-Apache) | text only | transformers + vLLM |
| **VLM pick** | `Qwen/Qwen3.5-0.8B` | 0.87B / 1.75 GB | Apache 2.0 | image + video; 262K ctx | vLLM >= 0.17 (`Qwen3_5ForConditionalGeneration`), transformers v5; MMLU-Pro 29.7, MMStar 58.3 |
| VLM alt (tiniest) | `HuggingFaceTB/SmolVLM-256M-Instruct` | 0.26B / 0.51 GB | Apache 2.0 | image | transformers (Idefics3); vLLM lists SmolVLM2/Idefics3 |
| VLM alt | `LiquidAI/LFM2.5-VL-450M` | 0.45B / 0.90 GB | LFM1.0 (commercial rights lapse at >= $10M revenue) | image | transformers >= 5.1, vLLM |

Recommended pair: **Qwen3-0.6B + Qwen3.5-0.8B** — same vendor, both Apache 2.0, ~3.3 GB
total, and Qwen3-0.6B genuinely has no image processor, so "image sent to a text-only
model is refused" is a real negative test, not a mock. Qwen3.5-0.8B is itself a VLM, so
it cannot double as the text-only model.

## Role C — vLLM vs transformers for a one-shot pod

| | vLLM 0.30.0 (2026-09-22) | transformers 5.18.0 (2026-09-30) |
|---|---|---|
| Install | 315 MB wheel; **pins `torch==2.13.0`**, torchvision 0.28, flashinfer; transformers >= 5.10.4; CUDA 12.9 wheels; CC >= 7.5. Official Docker image ~12 GB | 12.6 MB wheel, torch >= 2.5, any CUDA build — reuses the pod image's torch |
| Cold start | ~0.5B model 5-7 s with warm compile cache; torch.compile adds 8-12 s cold; `--enforce-eager` skips compile + CUDA graphs (arXiv 2606.07362) | import + load of a 0.6B model is a few seconds; no compile phase |
| Tiny-VLM support | Qwen3.5, LFM2-VL, Gemma 4, SmolVLM2/Idefics3, Qwen3-VL all listed | native (these are transformers-first models) |
| Throughput | PagedAttention, batching — matters for Role A | fine for one request |

Pragmatic call: kinoforge pods install at boot, so vLLM's torch pin collides with the
image's torch and adds a multi-GB download; `transformers` `generate()`
(`AutoModelForCausalLM` / `AutoModelForImageTextToText`) is the smoke-tier choice. vLLM
belongs behind the production config only if its quant kernels are needed (AWQ/FP8 at
24/48 GB). Hardware: 4090 (CC 8.9) has native FP8; A10 (CC 8.6) falls back to
FP8-Marlin W8A16; neither runs NVFP4.
