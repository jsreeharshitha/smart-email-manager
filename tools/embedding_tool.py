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
    vo = voyageai.Client(api_key=settings.VOYAGE_API_KEY)
    print(f"Generating vector embedding via Voyage AI using model {model}...")
    result = vo.embed([text], model=model, input_type="document")
    return result.embeddings[0]
