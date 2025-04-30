from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="cascadeui",
    version="1.1.0",
    author="HollowTheSilver",
    author_email="your.email@example.com",
    description="A Discord UI instance manager",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/HollowTheSilver/cascadeui",
    packages=find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.8",
    install_requires=[
        "discord.py>=2.0.0",
    ],
)
