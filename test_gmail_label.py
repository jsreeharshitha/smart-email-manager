import os
from tools.gmail_mcp import apply_label_to_email

def test_label_creation():
    """
    Manually tests the create_label tool functionality.
    """
    # 1. Check for credentials.json
    if not os.path.exists('credentials.json'):
        print("ERROR: 'credentials.json' not found in root directory.")
        print("Please download it from Google Cloud Console -> APIs & Services -> Credentials.")
        return

    apply_label_to_email("19e56c16e30555c2","Test Label - Gemini CLI")

if __name__ == "__main__":
    test_label_creation()
