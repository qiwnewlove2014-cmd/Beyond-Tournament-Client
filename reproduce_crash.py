
try:
    from accessible_output2 import outputs
    print("Import successful")
    speaker = outputs.auto.Auto()
    print("Speaker initialized successfully")
    speaker.output("Hello")
except Exception as e:
    print(f"Caught exception: {e}")
