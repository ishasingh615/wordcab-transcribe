# Copyright 2023 The Wordcab Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Transcribe Service for audio files."""

import math
import os
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple, Union

import numpy as np
import torch
import torch.nn.functional as F  # noqa N812
import torchaudio
from ctranslate2.models import WhisperGenerationResult
from faster_whisper import WhisperModel
from faster_whisper.tokenizer import Tokenizer
from faster_whisper.transcribe import (
    TranscriptionOptions,
    get_ctranslate2_storage,
    get_suppressed_tokens,
)
from loguru import logger
from torch.utils.data import DataLoader, IterableDataset

from wordcab_transcribe.logging import time_and_tell


# Word implementation from faster-whisper:
# https://github.com/guillaumekln/faster-whisper/blob/master/faster_whisper/transcribe.py#L24
class Word(NamedTuple):
    """Word unit for word_timestamps option."""

    start: float
    end: float
    word: str
    probability: float


class AudioDataset(IterableDataset):
    """Audio Dataset for transcribing audio files in batches."""

    def __init__(
        self,
        audio: Union[str, torch.Tensor],
        chunk_size: int,
        hop_length: int,
        mel_filters: torch.Tensor,
        n_fft: int,
        n_samples: int,
        sample_rate: int,
    ) -> None:
        """
        Initialize the Audio Dataset for transcribing audio files in batches.

        Args:
            audio (Union[str, torch.Tensor]): Audio file path or audio tensor.
            chunk_size (int): Size of audio chunks.
            hop_length (int): Hop length for the STFT.
            mel_filters (torch.Tensor): Mel filters to apply to the STFT.
            n_fft (int): Size of the FFT.
            n_samples (int): Number of samples to pad the audio.
            sample_rate (int): Sample rate of the audio.
        """
        self.chunk_size = chunk_size
        self.hop_length = hop_length
        self.n_fft = n_fft
        self.n_samples = n_samples
        self.mel_filters = mel_filters
        self.sample_rate = sample_rate

        if isinstance(audio, str):
            waveform = self.read_audio(audio)
        elif isinstance(audio, torch.Tensor):
            waveform = audio
        else:
            raise TypeError("Audio must be a string or a tensor.")

        (_audio_chunks, self.time_offsets, self.segment_durations) = self.create_chunks(
            waveform
        )

        self.features = [
            self._log_mel_spectrogram(chunk, padding=self.n_samples - chunk.shape[-1])
            for chunk in _audio_chunks
        ]

    def read_audio(self, filepath: str) -> torch.Tensor:
        """Read an audio file and return the audio tensor."""
        wav, sr = torchaudio.load(filepath)

        if wav.size(0) > 1:
            wav = wav.mean(dim=0, keepdim=True)

        if sr != self.sample_rate:
            transform = torchaudio.transforms.Resample(
                orig_freq=sr, new_freq=self.sample_rate
            )
            wav = transform(wav)
            sr = self.sample_rate

        return wav.squeeze(0)

    @time_and_tell
    def create_chunks(
        self, waveform: torch.Tensor
    ) -> Tuple[List[torch.Tensor], List[int], List[float]]:
        """Create 30-second chunks from the audio tensor."""
        num_segments = math.ceil(waveform.size(0) / self.n_samples)
        segments = [
            waveform[i * self.n_samples : (i + 1) * self.n_samples]
            for i in range(num_segments)
        ]

        time_offsets = [(i * self.chunk_size) for i in range(num_segments)]
        segment_durations = [
            self.chunk_size
            if len(segment) == self.n_samples
            else len(segment) / self.sample_rate
            for segment in segments
        ]

        return segments, time_offsets, segment_durations

    def __iter__(self) -> Dict[str, Union[torch.Tensor, float]]:
        """Iterate over the audio chunks and yield the features."""
        for feature, time_offset, segment_duration in zip(
            self.features, self.time_offsets, self.segment_durations
        ):
            yield {
                "feature": feature,
                "time_offset": time_offset,
                "segment_duration": segment_duration,
            }

    def _log_mel_spectrogram(
        self, audio: torch.Tensor, padding: int = 0
    ) -> torch.Tensor:
        """
        Compute the log-Mel spectrogram of a given audio tensor.

        Args:
            audio (torch.Tensor): Audio tensor of shape (n_samples,).
            padding (int): Number of samples to pad the audio.

        Returns:
            torch.Tensor: Log-Mel spectrogram of shape (n_mels, T).
        """
        if padding > 0:
            audio = F.pad(audio, (0, padding))

        window = torch.hann_window(self.n_fft).to(audio.device)
        stft = torch.stft(
            audio, self.n_fft, self.hop_length, window=window, return_complex=True
        )

        magnitudes = stft[..., :-1].abs() ** 2
        mel_spec = self.mel_filters @ magnitudes

        log_spec = torch.clamp(mel_spec, min=1e-10).log10()
        log_spec = torch.maximum(log_spec, log_spec.max() - 8.0)
        log_spec = (log_spec + 4.0) / 4.0

        return log_spec


class TranscribeService:
    """Transcribe Service for audio files."""

    def __init__(
        self,
        model_path: str,
        compute_type: str,
        device: str,
        num_workers: int,
    ) -> None:
        """Initialize the Transcribe Service.

        This service uses the WhisperModel from faster-whisper to transcribe audio files.

        Args:
            model_path (str): Path to the model checkpoint. This can be a local path or a URL.
            compute_type (str): Compute type to use for inference. Can be "int8", "int8_float16", "int16" or "float_16".
            device (str): Device to use for inference. Can be "cpu" or "cuda".
            num_workers (int): Number of workers to use for inference.
        """
        self.model = WhisperModel(
            model_path,
            device=device,
            compute_type=compute_type,
            num_workers=num_workers,
        )
        self.tokenizer = Tokenizer(
            self.model.hf_tokenizer,
            self.model.model.is_multilingual,
            task="transcribe",
            language="en",  # Default language, to gain some speed
        )

        self._batch_size = 8  # TODO: Make this configurable
        self.sample_rate = 16000

        self.n_fft = 400
        self.n_mels = 80
        self.chunk_size = 30
        self.hop_length = 160

        self.n_samples = self.sample_rate * self.chunk_size

        assets_dir = Path(__file__).parent.parent / "assets" / "mel_filters.npz"
        with np.load(str(assets_dir)) as f:
            self.mel_filters = torch.from_numpy(f[f"mel_{self.n_mels}"])

        self.options = {
            "beam_size": 5,
            "best_of": 5,
            "patience": 1,
            "length_penalty": 1,
            "log_prob_threshold": -1.0,
            "no_speech_threshold": 0.6,
            "compression_ratio_threshold": 2.4,
            "condition_on_previous_text": True,
            "temperatures": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            "initial_prompt": None,
            "prefix": None,
            "suppress_blank": True,
            "suppress_tokens": get_suppressed_tokens(self.tokenizer, [-1]),
            "without_timestamps": False,
            "max_initial_timestamp": 1.0,
            "word_timestamps": False,
            "prepend_punctuations": "\"'“¿([{-",
            "append_punctuations": "\"'.。,，!！?？:：”)]}、",
        }

    def __call__(
        self,
        audio: Union[str, torch.Tensor],
        source_lang: str,
        **kwargs: Optional[dict],
    ) -> List[dict]:
        """
        Run inference with the transcribe model.

        Args:
            audio (Union[str, torch.Tensor]): Audio file to transcribe.
            source_lang (str): Language of the audio file.
            kwargs (Any): Additional arguments to pass to TranscribeService.

        Returns:
            List[dict]: List of segments with the following keys: "start", "end", "text", "confidence".
        """
        if kwargs.get("faster_whisper", False):
            segments, _ = self.model.transcribe(
                audio, language=source_lang, word_timestamps=True
            )

            outputs = [segment._asdict() for segment in segments]

        else:
            if self.tokenizer.language_code != source_lang:
                self.tokenizer = Tokenizer(
                    self.model.hf_tokenizer,
                    self.model.model.is_multilingual,
                    task="transcribe",
                    language=source_lang,
                )

            _options = self.options.copy()
            _options.update(kwargs)
            options = TranscriptionOptions(**_options)

            outputs = self.pipeline(audio, batch_size=self._batch_size, options=options)

        return outputs

    @time_and_tell
    def pipeline(
        self,
        audio: Union[str, torch.Tensor],
        batch_size: int,
        options: TranscriptionOptions,
    ) -> List[dict]:
        """
        Transcription pipeline for audio chunks in batches.

        Args:
            audio (Union[str, torch.Tensor]): Audio file to transcribe.
            batch_size (int): Batch size to use for inference.
            options (TranscriptionOptions): Transcription options.

        Returns:
            List[dict]: List of segments with the following keys: "start", "end", "text".
        """
        dataset = AudioDataset(
            audio=audio,
            chunk_size=self.chunk_size,
            hop_length=self.hop_length,
            mel_filters=self.mel_filters,
            n_fft=self.n_fft,
            n_samples=self.n_samples,
            sample_rate=self.sample_rate,
        )
        dataloader = DataLoader(
            dataset,
            batch_size=batch_size,
            collate_fn=self._collate_fn,
        )

        outputs = []
        for batch in dataloader:
            batch_outputs = self._generate_segment_batched(
                features=batch["features"],
                time_offsets=batch["time_offsets"],
                segment_durations=batch["segment_durations"],
                tokenizer=self.tokenizer,
                options=options,
            )
            outputs.extend(batch_outputs)

        return outputs

    # This is an adapted version of the faster-whisper transcription pipeline:
    # https://github.com/guillaumekln/faster-whisper/blob/master/faster_whisper/transcribe.py
    @time_and_tell
    def _generate_segment_batched(
        self,
        features: torch.Tensor,
        time_offsets: List[float],
        segment_durations: List[float],
        tokenizer: Tokenizer,
        options: TranscriptionOptions,
    ) -> List[dict]:
        """
        Use the ctranslate2 Whisper model to generate text from audio chunks.

        Args:
            features (torch.Tensor): List of audio chunks.
            time_offsets (List[float]): Time offsets for the audio chunks.
            segment_durations (List[float]): Durations of the audio chunks.
            tokenizer (Tokenizer): Tokenizer to use for encoding the text.
            options (TranscriptionOptions): Options to use for transcription.

        Returns:
            List[dict]: List of segments with the following keys: "start", "end", "text", "confidence".
        """
        if "TOKENIZERS_PARALLELISM" not in os.environ:
            os.environ["TOKENIZERS_PARALLELISM"] = "false"

        batch_size = features.size(0)
        logger.debug(f"Batch size: {batch_size}")

        all_tokens = []
        prompt_reset_since = 0

        if options.initial_prompt is not None:
            initial_prompt = " " + options.initial_prompt.strip()
            initial_prompt_tokens = tokenizer.encode(initial_prompt)
            all_tokens.extend(initial_prompt_tokens)

        previous_tokens = all_tokens[prompt_reset_since:]
        prompt = self.model.get_prompt(
            tokenizer,
            previous_tokens,
            without_timestamps=options.without_timestamps,
            prefix=options.prefix,
        )

        features = get_ctranslate2_storage(features)

        # TODO: Could be better to get the results as np.ndarray/torch.tensor and not as a class for speed
        # Atm, we need to extract the results as a Python list which is slow because we get this results:
        # https://opennmt.net/CTranslate2/python/ctranslate2.models.WhisperGenerationResult.html
        # TODO: We access the inherited ctranslate2 model for generation here. This is not ideal.
        result: WhisperGenerationResult = self.model.model.generate(
            features,
            [prompt] * batch_size,
            beam_size=options.beam_size,
            patience=options.patience,
            length_penalty=options.length_penalty,
            return_scores=False,
            return_no_speech_prob=False,
            suppress_blank=False,
        )

        outputs = []
        for res, time_offset, segment_duration in zip(
            result, time_offsets, segment_durations
        ):
            tokens = res.sequences_ids[0]
            current_segments = []

            single_timestamp_ending = (
                len(tokens) >= 2
                and tokens[-2] < tokenizer.timestamp_begin
                and tokens[-1] >= tokenizer.timestamp_begin
            )

            consecutive_timestamps = [
                i
                for i in range(len(tokens))
                if i > 0
                and tokens[i] >= tokenizer.timestamp_begin
                and tokens[i - 1] >= tokenizer.timestamp_begin
            ]

            if len(consecutive_timestamps) > 0:
                slices = list(consecutive_timestamps)
                if single_timestamp_ending:
                    slices.append(len(tokens))

                last_slice = 0
                for current_slice in slices:
                    sliced_tokens = tokens[last_slice:current_slice]
                    start_timestamp_position = (
                        sliced_tokens[0] - tokenizer.timestamp_begin
                    )
                    end_timestamp_position = (
                        sliced_tokens[-1] - tokenizer.timestamp_begin
                    )
                    start_time = time_offset + start_timestamp_position * 0.02
                    end_time = time_offset + end_timestamp_position * 0.02

                    current_segments.append(
                        dict(
                            start=start_time,
                            end=end_time,
                            tokens=sliced_tokens,
                        )
                    )
                    last_slice = current_slice
            else:
                duration = segment_duration
                timestamps = [
                    token for token in tokens if token >= tokenizer.timestamp_begin
                ]
                if len(timestamps) > 0 and timestamps[-1] != tokenizer.timestamp_begin:
                    last_timestamp_position = timestamps[-1] - tokenizer.timestamp_begin
                    duration = last_timestamp_position * 0.02

                current_segments.append(
                    dict(
                        start=time_offset,
                        end=time_offset + duration,
                        tokens=tokens,
                    )
                )

            # TODO: Implement word timestamps
            # if options.word_timestamps:
            # segment_size = segment_duration / (self._hop_length / self.sample_rate)
            # self.model.add_word_timestamps(
            #     current_segments,
            #     tokenizer,
            #     features,
            #     segment_size,
            #     options.prepend_punctuations,
            #     options.append_punctuations,
            # )

            for segment in current_segments:
                outputs.append(
                    {
                        "start": segment["start"],
                        "end": segment["end"],
                        "tokens": segment["tokens"],
                        "words": None,
                        # "words": (
                        #     [Word(**word) for word in segment["words"]]
                        #     if options.word_timestamps
                        #     else None
                        # )
                    }
                )

        decoded_outputs = self._decode_batch(outputs)

        return decoded_outputs

    def _decode_batch(self, outputs: List[dict]) -> List[dict]:
        """
        Extract the token ids from the sequences ids and decode them using the tokenizer.

        Args:
            outputs (List[dict]): List of outputs from the model.

        Returns:
            List[str]: List of decoded texts.
        """
        tokens_to_decode = [
            [token for token in out["tokens"] if token < self.tokenizer.eot]
            for out in outputs
        ]
        # TODO: We call the inherited tokenizer here, because faster_whisper tokenizer
        # doesn't have the decode_batch method. We should fix this in the future.
        decoded_tokens = self.tokenizer.tokenizer.decode_batch(tokens_to_decode)

        for out, token in zip(outputs, decoded_tokens):
            out["text"] = token

        return outputs

    def _collate_fn(
        self, items: List[Dict[str, Union[torch.Tensor, List[float]]]]
    ) -> Dict[str, Union[torch.Tensor, List]]:
        """
        Collator function for the dataloader.

        Args:
            items (List[Dict[str, Union[torch.Tensor, List[float]]]]): List of items to collate.

        Returns:
            torch.tensor: Collated items.
        """
        collated_items = {
            "features": torch.stack([item["feature"] for item in items]),
            "time_offsets": [item["time_offset"] for item in items],
            "segment_durations": [item["segment_duration"] for item in items],
        }

        return collated_items
