from setuptools import setup


setup(
    name="personal-dev-agent",
    version="0.1.0",
    package_dir={"": "src"},
    packages=["dev_agent"],
    install_requires=["PyJWT[crypto]>=2.10"],
    package_data={"dev_agent": ["web/*.html", "web/*.css", "web/*.js"]},
    entry_points={
        "console_scripts": [
            "dev-agent=dev_agent.cli:main",
            "dev-agent-web=dev_agent.server:main",
        ]
    },
)
