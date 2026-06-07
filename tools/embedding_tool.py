import voyageai
from config import settings

def generate_embedding(text: str, model: str = "voyage-3"):
    """
    Generates a vector embedding for the given text using Voyage AI.
    
    Args:
        text (str): The text to embed.
        model (str): The Voyage AI model to use.
        
    Returns:
        list: The vector embedding.
    """
    if not text or not text.strip():
        print(f"[*] WARNING: Skipping embedding for empty/whitespace input.")
        return [0.0] * 1024 # Standard dimension for voyage-3

    vo = voyageai.Client(api_key=settings.VOYAGE_API_KEY)
    print(f"Generating vector embedding via Voyage AI using model {model}...")
    result = vo.embed([text], model=model, input_type="document")
    return result.embeddings[0]
