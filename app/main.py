from fastapi import FastAPI

app = FastAPI(
    title="ARISE Nutrition Service",
    description="Service de gestion des repas, produits et prix pour ARISE.",
    version="1.0.0",
)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "message": "ARISE Nutrition Service is running",
    }


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {
        "status": "healthy",
    }