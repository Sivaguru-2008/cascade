# Contributing to CASCADE

Thank you for your interest in contributing to **CASCADE** (Confidential AI Surveillance of Clinical Adverse Cascades & Drug Events), built for the IBM Z Datathon 2026.

## Development Workflow

### 1. Environment Setup

CASCADE requires Python 3.11+.

```bash
git clone https://github.com/Sivaguru-2008/cascade.git
cd cascade
python -m venv .venv
source .venv/bin/activate  # Or on Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Generating Synthetic Verification Data

All clinical data used in development is synthetic:

```bash
python -m datagen.generate --patients 20000 --seed 7
python -m loop1.train
```

### 3. Running Automated Tests

```bash
pytest -v
```

All 87 tests must pass before submitting any pull request.

### 4. Code Standards & Linting

We enforce clean formatting using `ruff`:

```bash
ruff check .
```

### 5. Running on IBM LinuxONE (s390x)

To verify on LinuxONE s390x architecture:

```bash
./deploy/run_all.sh --generate --train
./deploy/evidence.sh
```

## Security & Ethics Guidelines

- **Synthetic Only:** Never commit real-world patient, pharmacy, or prescriber health information (PHI).
- **Zero-Knowledge Principle:** Patient and physician identifiers must always be transformed into HMAC-SHA256 tokens at the edge layer.

## License

By contributing, you agree that your contributions will be licensed under the [Apache-2.0 License](LICENSE).
