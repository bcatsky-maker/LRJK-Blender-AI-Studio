from src.core.generator import AIGenerator

def test_ai_generator():
    generator = AIGenerator(model_name="LRJK-Gen-v2")
    result = generator.generate_terrain(seed=42, resolution=512)
    assert "Generated terrain" in result
    assert "42" in result