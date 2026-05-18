from faster_whisper import WhisperModel


def main() -> None:
    model_size = "large-v3"

    print(f"Loading Whisper model on CUDA: {model_size}")

    model = WhisperModel(
        model_size,
        device="cuda",
        compute_type="float16",
    )

    print("Model loaded successfully on CUDA.")

    # This only checks model loading.
    # Later we will add real audio transcription.


if __name__ == "__main__":
    main()