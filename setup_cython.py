import sys
from setuptools import setup, Extension
from Cython.Build import cythonize

modules_to_cythonize = [
    "src/core/generator.py",
]

ext_modules = [
    Extension(
        name=mod.replace("/", ".").replace("\\", ".").replace(".py", ""),
        sources=[mod]
    )
    for mod in modules_to_cythonize
]

setup(
    name="LRJK_Blender_AI_Studio_Core",
    ext_modules=cythonize(
        ext_modules,
        compiler_directives={'language_level': "3", 'always_allow_keywords': True},
        quiet=False
    ),
)