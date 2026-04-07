import speech_recognition as sr
import time

def test_mic():
    r = sr.Recognizer()
    print("Microphones found:")
    for i, name in enumerate(sr.Microphone.list_microphone_names()):
        print(f"[{i}] {name}")
    
    print("\nListening for 5 seconds to test sensitivity...")
    try:
        with sr.Microphone() as source:
            r.adjust_for_ambient_noise(source, duration=2)
            print(f"Energy threshold set to: {r.energy_threshold}")
            audio = r.listen(source, timeout=5, phrase_time_limit=5)
            print("Detected something! Trying to recognize...")
            text = r.recognize_google(audio)
            print(f"Heard: {text}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_mic()
