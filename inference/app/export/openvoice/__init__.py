"""Vendored, trimmed OpenVoice V2 tone-color-converter model code.

Source: https://github.com/myshell-ai/OpenVoice (MIT License, Copyright 2024
MyShell.ai). Only the voice-conversion inference path is kept (posterior
encoder, flow, HiFi-GAN decoder, reference encoder); the TTS text encoder and
duration predictors are dropped. Module/attribute names match upstream so the
published converter checkpoint (``myshell-ai/OpenVoiceV2`` on Hugging Face)
loads directly via ``load_state_dict``.

Requires torch — install the ``export`` dependency group. The live inference
service never imports this package.
"""
