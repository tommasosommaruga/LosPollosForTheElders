# Source provenance

This project combines:

- Vision and integration code from [keremhamdi/Robot_Vision](https://github.com/keremhamdi/Robot_Vision).
- A locally modified controller from [ciloglu44/lospollos-arm](https://github.com/ciloglu44/lospollos-arm),
  pinned to `c8b9797754ff0dbd4f1074ce155c11d3d4378062`.

Controller modifications include a versioned vision contract, structured grasp
results, observation metadata, read-only mode and stricter feedback checks.
Upstream robot calibration, saved poses and movements are not included.
Existing source authorship is retained; no new blanket license is assigned to
the combined code. Each upstream component and model remains subject to its own
terms. This repository does not redistribute SAM 3, Qwen or NormaCore weights/runtime.

SAM 3 is loaded through the Hugging Face Transformers `Sam3Model` and
`Sam3Processor` integration. See the [official SAM 3 documentation](https://huggingface.co/docs/transformers/model_doc/sam3).
