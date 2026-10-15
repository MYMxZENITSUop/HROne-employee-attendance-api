# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management, attendance tracking, attendance regularization, and analytics.

## Requirements

- Python 3.10+
- MongoDB 6.0 or newer
- Dependencies listed in `requirements.txt`

## Setup

Create and activate a virtual environment.

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create a `.env` file locally using `.env.example` as a reference and configure:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Start MongoDB, then load the supplied sample data if required:

```bash
python sample_seed.py
```

## Run the API

```bash
uvicorn app.main:app --port 8000
```

For development with automatic reload:

```bash
uvicorn app.main:app --port 8000 --reload
```

Interactive API documentation is available at:

`http://127.0.0.1:8000/docs`

## Main Features

- Employee creation and listing
- Attendance punch-in and punch-out
- Attendance listing with filters and pagination
- Attendance regularization with audit history
- Monthly employee analytics
- Department summary and attendance trends
- Late-arrival leaderboard
- Query-plan inspection through explain endpoints
- MongoDB indexes for uniqueness and query performance

## Configuration and Security

Configure MongoDB using environment variables. Do not commit `.env`, credentials, or other secrets to the public repository.

## Submission

The repository includes `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this README.