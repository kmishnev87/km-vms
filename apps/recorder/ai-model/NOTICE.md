# Bundled local detector

`yolox_tiny.onnx` comes from the [official YOLOX 0.1.1rc0 release](https://github.com/Megvii-BaseDetection/YOLOX/releases/tag/0.1.1rc0). The Docker build and the detector check the artifact against the content hash in `model.json`. The original YOLOX Apache-2.0 license is included in the image as `LICENSE-YOLOX.txt`. KM VMS supplies the native IPC, preprocessing, COCO class grouping and bounded rules; it does not retrain or modify the weights.

OpenVINO (Apache-2.0), ONNX Runtime (MIT), their dependency licenses and third-party notices are supplied by the pinned Python wheels. The runtime does not fetch models or send frames to a service.

Linux amd64 includes the checksummed official [Intel Compute Runtime](https://github.com/intel/compute-runtime/releases/tag/25.35.35096.9), gmmlib and [Intel Graphics Compiler](https://github.com/intel/intel-graphics-compiler/releases/tag/v2.18.5) user-space packages in `/opt/kmvms-intel`. They keep their copyright notices and are only loaded by the AI worker. No host driver or FFmpeg library is replaced.
