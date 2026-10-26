from setuptools import setup, find_packages

setup(
    name="hpc_eff",
    version="0.6",
    description="Energy Optimization Governor",
    long_description_content_type="text/markdown",
    author="CESNET",
    license="BSD-3-Clause",
    python_requires=">=3.9",
    install_requires=[
        "numpy",
        "requests",
    ],
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    include_package_data=True,
    keywords=["governor", "cpu", "frequency", "carbon emission"],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: BSD 3-Clause License",
    ],
    entry_points={
        "console_scripts": [
            "hpc-eff = hpc_eff.main:main",
        ],
    },
    project_urls={
        "Repository": "https://github.com/CESNET/hpc_eff"
    },
)
