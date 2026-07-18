# Licenses and third-party notices

`irodori voice studio` is distributed under the MIT License. See `LICENSE`.

Downloaded components remain under their own licenses:

- [audio.cpp](https://github.com/0xShug0/audio.cpp): Apache License 2.0. The setup downloads the upstream Windows Release without modification. Its license is available at the upstream repository and is copied to `runtime/licenses/` during a full setup.
- [Irodori-TTS-500M-v3](https://huggingface.co/Aratako/Irodori-TTS-500M-v3): MIT License. The model card also prohibits unauthorized impersonation, fraud, deepfakes, and misinformation.
- [audio.cpp standalone Irodori-TTS GGUF](https://huggingface.co/audio-cpp/audio.cpp-gguf): model contents originate from the MIT-licensed Irodori-TTS model.
- NVIDIA CUDA runtime files, when selected: governed by the [NVIDIA CUDA Toolkit EULA](https://docs.nvidia.com/cuda/eula/). Only the runtime package published with the official audio.cpp Release is downloaded.
- Python: [Python Software Foundation License](https://docs.python.org/3/license.html).
- Python packages installed from PyPI retain their respective licenses. Package metadata is available with `python -m pip show numpy requests soundfile`.

The setup screen requires confirmation before downloading or installing anything. Reference WAV files, optional LLM models, user character files, generated audio, and local settings are not distributed.
