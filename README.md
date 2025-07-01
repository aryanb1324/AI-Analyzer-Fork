# Ask Your Spreadsheet

This project is a simple Flask application that lets users upload an Excel spreadsheet and ask natural language questions about the data. It uses the OpenAI API to generate answers based on a summary of the uploaded file.

## Features
- Upload `.xls` or `.xlsx` files
- Automatic data summary with basic error detection and trend analysis
- Ask follow-up questions in a chat interface

## Installation
1. Clone this repository.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a `.env` file in the project root with your OpenAI API key:
   ```bash
   OPENAI_API_KEY=your-key-here
   ```
4. Run the app:
   ```bash
   python main.py
   ```

Visit `http://localhost:5000` to use the tool.

## Example Questions
- "What is the total revenue for 2022?"
- "Are there any errors in this model?"
- "Explain how depreciation is calculated here."

## Architecture
```
Excel file -> Flask backend -> Data summary & OpenAI -> Response -> Frontend
```

