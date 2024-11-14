from pathlib import Path

BASE_PATH = Path("/scratch/shared/rag_drias/")  # to be adapted by user

BASE_URL = "https://www.drias-climat.fr"
MENU_URL = f"{BASE_URL}/accompagnement/getAllTopSectionsJson"
SECTION_URL = f"{BASE_URL}/accompagnement/sections"

PATH_DATA = BASE_PATH / "text_data"
PATH_MENU_JSON = PATH_DATA / "getAllTopSectionsJson.json"
PATH_VDB = BASE_PATH / "chroma_database"
PATH_MODELS = BASE_PATH / "hf_models"
