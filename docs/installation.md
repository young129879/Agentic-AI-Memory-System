# Installation Guide

## Requirements

- **Python**: 3.10 or higher
- **OpenAI API Key**: Required for GPT-4.1-mini access
- **Operating System**: Cross-platform (Windows, macOS, Linux)

## Installation Options

### Option 1: Install from PyPI (Recommended)

```bash
pip install hmlr
```

**With optional dependencies:**

```bash
# For LangChain integration
pip install hmlr[langchain]

# For telemetry support (Arize Phoenix)
pip install hmlr[telemetry]

# For development and testing
pip install hmlr[dev]

# Install all extras
pip install hmlr[langchain,telemetry,dev]
```

### Option 2: Install from Source

```bash
git clone https://github.com/Sean-V-Dev/HMLR-Agentic-AI-Memory-System.git
cd HMLR-Agentic-AI-Memory-System
pip install -e .
```

**With optional dependencies:**

```bash
pip install -e .[langchain,telemetry,dev]
```

## Environment Setup

Create a `.env` file in your project directory:

```bash
OPENAI_API_KEY=your-openai-api-key-here
```

**Optional variables:**

```bash
# For test result tracking
LANGSMITH_API_KEY=your-langsmith-key-here

# For telemetry (if using [telemetry] extra)
PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006
```

## Verify Installation

```python
from hmlr import HMLRClient

# Should import without errors
print("HMLR installed successfully!")
```

## What Gets Installed

### Core Dependencies
- `openai>=1.0.0` - OpenAI API client
- `sentence-transformers>=2.2.0` - Embedding generation
- `numpy>=1.24.0` - Numerical operations

### Optional Dependencies

**`[langchain]`**:
- `langchain>=0.1.0`
- `langchain-openai>=0.1.0`

**`[telemetry]`**:
- `arize-phoenix>=4.0.0`
- `opentelemetry-api>=1.20.0`
- `opentelemetry-sdk>=1.20.0`

**`[dev]`**:
- `pytest>=7.0.0`
- `pytest-asyncio>=0.21.0`
- `ragas>=0.4.0`
- `langsmith>=0.2.0`
- `datasets>=2.14.0`
- `python-dotenv>=1.0.0`

## Troubleshooting

### Import Errors

If you get import errors, ensure you're using Python 3.10+:

```bash
python --version
```

### Missing API Key

If you see "API key not found", ensure your `.env` file is in the correct directory or set the environment variable:

```bash
export OPENAI_API_KEY="your-key-here"  # Linux/macOS
set OPENAI_API_KEY="your-key-here"     # Windows CMD
$env:OPENAI_API_KEY="your-key-here"   # Windows PowerShell
```

### Database Permissions

HMLR creates SQLite databases. Ensure you have write permissions in the directory where you run your code.

## Next Steps

- See [Quickstart Guide](quickstart.md) for basic usage
- Review [Model Compatibility](model_compatibility.md) for important model warnings
- Check out [examples/](../examples/) for sample code
