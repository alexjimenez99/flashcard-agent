# Pull Python 3.10
FROM public.ecr.aws/lambda/python:3.10

# Copy requirements
COPY requirements.txt requirements.txt

# Copy prompts
COPY prompts.py prompts.py 

# Copy Agents
COPY agents.py agents.py 

# Copy Utils
COPY utils.py utils.py

# Copy Pydantic Validation
COPY pydantic_formatting.py pydantic_formatting.py

# Install dependencies
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Copy your app code (update as needed)
COPY lambda_function.py lambda_function.py

# Launch your server (e.g., uvicorn for FastAPI)
CMD ["lambda_function.lambda_function"]
