"""Flask web application for querying Excel spreadsheets with GPT."""

from flask import Flask, render_template, request, flash, redirect, url_for, session, jsonify
import os
import pandas as pd
import numpy as np
from werkzeug.utils import secure_filename
from openai import OpenAI
from dotenv import load_dotenv
import openpyxl
from datetime import datetime
import re
import uuid
import pickle
import tempfile

load_dotenv()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024 * 1024
app.config['SECRET_KEY'] = 'your-secret-key-here'

# In-memory store for uploaded file information and conversation history
file_store = {}

client = OpenAI()

ALLOWED_EXTENSIONS = {'xls', 'xlsx'}

def allowed_file(filename):
    """Check if the uploaded file has an allowed extension"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def detect_data_errors(df):
    """Detect common data errors in the spreadsheet"""
    errors = []
    
    for col in df.columns:
        for idx, value in enumerate(df[col]):
            if pd.isna(value):
                continue
            if isinstance(value, str):
                excel_errors = ['#DIV/0!', '#N/A', '#NAME?', '#NULL!', '#NUM!', '#REF!', '#VALUE!']
                if any(error in str(value) for error in excel_errors):
                    errors.append(f"Excel error '{value}' found in column '{col}' at row {idx + 2}")
    
    numeric_cols = df.select_dtypes(include=[np.number]).columns
    for col in numeric_cols:
        missing_count = df[col].isna().sum()
        if missing_count > 0:
            missing_percentage = (missing_count / len(df)) * 100
            if missing_percentage > 10:
                errors.append(f"Column '{col}' has {missing_count} missing values ({missing_percentage:.1f}%)")

    for col in numeric_cols:
        if len(df[col].dropna()) > 0:
            Q1 = df[col].quantile(0.25)
            Q3 = df[col].quantile(0.75)
            IQR = Q3 - Q1
            lower_bound = Q1 - 1.5 * IQR
            upper_bound = Q3 + 1.5 * IQR
            outliers = df[(df[col] < lower_bound) | (df[col] > upper_bound)][col]
            if len(outliers) > 0:
                errors.append(f"Column '{col}' has {len(outliers)} potential outliers (values significantly outside normal range)")
    
    return errors

def analyze_time_series_trends(df):
    """Analyze trends in time-series data"""
    trends = []
    
    date_columns = []
    for col in df.columns:
        if df[col].dtype == 'datetime64[ns]' or 'date' in col.lower() or 'time' in col.lower():
            date_columns.append(col)
    
    if not date_columns and len(df.columns) > 0:
        first_col = df.columns[0]
        try:
            pd.to_datetime(df[first_col].head(5), errors='raise')
            date_columns.append(first_col)
        except:
            pass
    
    if date_columns:
        date_col = date_columns[0]
        if df[date_col].dtype != 'datetime64[ns]':
            try:
                df[date_col] = pd.to_datetime(df[date_col])
            except:
                return trends
        
        numeric_cols = df.select_dtypes(include=[np.number]).columns
        for col in numeric_cols:
            if len(df[col].dropna()) >= 3:
                df_sorted = df.sort_values(date_col)
                values = df_sorted[col].dropna()
                
                if len(values) >= 3:
                    first_val = values.iloc[0]
                    last_val = values.iloc[-1]
                    change = last_val - first_val
                    change_percent = (change / first_val * 100) if first_val != 0 else 0
                    
                    max_val = values.max()
                    min_val = values.min()
                    
                    trend_direction = "increasing" if change > 0 else "decreasing" if change < 0 else "stable"
                    
                    trends.append({
                        'column': col,
                        'direction': trend_direction,
                        'start_value': first_val,
                        'end_value': last_val,
                        'change': change,
                        'change_percent': change_percent,
                        'peak': max_val,
                        'minimum': min_val
                    })
    
    return trends

def create_enhanced_data_summary(df, file_info, file_obj=None):
    """Create an enhanced summary with error detection and trend analysis"""
    
    summary = f"""
EXCEL DATA ANALYSIS REPORT
==========================
File: {file_info['filename']}
Dimensions: {file_info['rows']:,} rows × {file_info['columns']} columns
Sheets: {', '.join(file_info['sheet_names'])}
Columns: {', '.join(file_info['column_names'])}
"""
    
    errors = detect_data_errors(df)
    if errors:
        summary += f"\n⚠️  DATA QUALITY ISSUES DETECTED:\n"
        for error in errors[:5]:
            summary += f"- {error}\n"
        if len(errors) > 5:
            summary += f"... and {len(errors) - 5} more issues\n"
    else:
        summary += f"\n✅ No major data quality issues detected\n"
    
    trends = analyze_time_series_trends(df.copy())
    if trends:
        summary += f"\n📈 TREND ANALYSIS:\n"
        for trend in trends[:3]:
            summary += f"- {trend['column']}: {trend['direction'].upper()} trend "
            summary += f"({trend['start_value']:.2f} → {trend['end_value']:.2f}, "
            summary += f"{trend['change_percent']:+.1f}%)\n"
            summary += f"  Peak: {trend['peak']:.2f}, Minimum: {trend['minimum']:.2f}\n"
            
    if file_info.get('numeric_stats'):
        summary += f"\n📊 NUMERIC SUMMARY:\n"
        for col, stats in list(file_info['numeric_stats'].items())[:5]:
            summary += f"- {col}: Sum={stats['sum']:,.2f}, Avg={stats['mean']:.2f}, Count={stats['count']}\n"
    
    if len(df) <= 10:
        summary += f"\n📋 COMPLETE DATASET:\n{df.to_string()}\n"
    else:
        summary += f"\n📋 SAMPLE DATA (first 5 rows):\n{df.head(5).to_string()}\n"
    
    return summary, errors, trends

def ask_openai_with_enhanced_context(data_summary, user_question, errors, trends, qa_history=None, model="gpt-3.5-turbo"):
    """Send the user's question and data summary to OpenAI with extra context."""
    try:
        context_additions = []
        
        if errors:
            context_additions.append(f"IMPORTANT: This spreadsheet contains {len(errors)} data quality issues that may affect analysis accuracy.")
        
        if trends:
            trend_summary = "Key trends identified: " + ", ".join([f"{t['column']} is {t['direction']}" for t in trends[:3]])
            context_additions.append(trend_summary)
        
        question_lower = user_question.lower()
        if any(word in question_lower for word in ['trend', 'growth', 'increase', 'decrease', 'change']):
            context_additions.append("The user is asking about trends - focus on the trend analysis data provided above.")
        
        if any(word in question_lower for word in ['error', 'problem', 'issue', 'wrong']):
            context_additions.append("The user is asking about data quality - reference the data quality issues section above.")
        
        if any(word in question_lower for word in ['total', 'sum', 'average', 'mean', 'max', 'min']):
            context_additions.append("The user wants statistical calculations - use the numeric summary data provided.")
        
        enhanced_context = "\n".join(context_additions) if context_additions else ""
        

        history_context = ""
        if qa_history:
            history_context = "\nPREVIOUS CONVERSATION:\n"
            for i, qa in enumerate(qa_history[-3:], 1): 
                history_context += f"Q{i}: {qa['question']}\nA{i}: {qa['answer'][:200]}...\n\n"
        
        prompt = f"""You are an expert financial data analyst. Analyze the Excel data below and answer the user's question with professional insights.

{data_summary}

ANALYSIS GUIDANCE:
{enhanced_context}

{history_context}

User's Current Question: {user_question}

Instructions:
1. Provide a clear, specific answer based on the data above
2. Consider the conversation history when relevant
3. If data quality issues exist, mention how they might affect your analysis
4. Use actual numbers from the data when possible
5. If the question cannot be fully answered, explain what information is available
6. For trend questions, reference the trend analysis
7. For financial data, consider business implications
8. Keep your response professional but accessible

Answer:"""

        chat_completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a professional financial data analyst with expertise in Excel analysis, trend identification, and business intelligence. You maintain context from previous questions about the same dataset."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=600,
        )
        return chat_completion.choices[0].message.content.strip()
    except Exception as e:
        return f"Error analyzing data: {str(e)}. Please check your OpenAI API configuration."

def store_file_data(df, file_info, errors, trends, data_summary):
    """Store dataframe and metadata in memory under a unique identifier."""
    file_id = str(uuid.uuid4())
    file_store[file_id] = {
        'dataframe': df,
        'file_info': file_info,
        'errors': errors,
        'trends': trends,
        'data_summary': data_summary,
        'qa_history': [],
        'timestamp': datetime.now()
    }
    return file_id

def get_file_data(file_id):
    """Retrieve stored file data"""
    return file_store.get(file_id)

def add_qa_to_history(file_id, question, answer):
    """Add Q&A to conversation history"""
    if file_id in file_store:
        file_store[file_id]['qa_history'].append({
            'question': question,
            'answer': answer,
            'timestamp': datetime.now()
        })

@app.route('/')
def index():
    """Render the main page"""
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    """Process uploaded Excel file and send first question to OpenAI."""
    if 'file' not in request.files:
        flash('Please choose an Excel file to upload.')
        return redirect(url_for('index'))
    
    file = request.files['file']
    query = request.form.get('question', '').strip()
    
    if file.filename == '':
        flash('Please choose an Excel file to upload.')
        return redirect(url_for('index'))
    
    if not query:
        flash('Enter a question about your spreadsheet so the AI knows what to analyze.')
        return redirect(url_for('index'))
    
    if not allowed_file(file.filename):
        flash('Unsupported file format. Upload a .xls or .xlsx spreadsheet.')
        return redirect(url_for('index'))
    
    try:
        filename = secure_filename(file.filename)
        print(f"Processing: {filename} with question: {query}")
        
        file_extension = filename.rsplit('.', 1)[1].lower()
        engine = 'openpyxl' if file_extension == 'xlsx' else 'xlrd'
        
        df = pd.read_excel(file, engine=engine)
        
        numeric_stats = {}
        for col in df.select_dtypes(include=['number']).columns:
            numeric_stats[col] = {
                'sum': float(df[col].sum()),
                'mean': float(df[col].mean()),
                'count': int(df[col].count())
            }
        
        try:
            xl_file = pd.ExcelFile(file, engine=engine)
            sheet_names = xl_file.sheet_names
        except:
            sheet_names = ['Sheet1']
        
        file_info = {
            'filename': filename,
            'rows': len(df),
            'columns': len(df.columns),
            'column_names': list(df.columns),
            'sheet_names': sheet_names,
            'numeric_stats': numeric_stats
        }
        
        data_summary, errors, trends = create_enhanced_data_summary(df, file_info, file)
        

        file_id = store_file_data(df, file_info, errors, trends, data_summary)
        
        print("Sending enhanced request to OpenAI...")
        ai_answer = ask_openai_with_enhanced_context(data_summary, query, errors, trends)
        print(f"AI Response received: {len(ai_answer)} characters")
        

        add_qa_to_history(file_id, query, ai_answer)
        
        display_df = df.head(100) if len(df) > 100 else df
        file_data = display_df.to_html(classes='table table-bordered table-striped', 
                                      index=False, 
                                      table_id='data-table')
        
        return render_template('chat.html', 
                             file_data=file_data, 
                             file_info=file_info,
                             errors=errors,
                             trends=trends,
                             file_id=file_id,
                             qa_history=file_store[file_id]['qa_history'])
    
    except Exception as e:
        print(f"Error processing file: {str(e)}")
        flash("We couldn't read your Excel file. Try uploading a .xlsx file with data in the first sheet.")
        return redirect(url_for('index'))

@app.route('/ask_question', methods=['POST'])
def ask_question():
    """Answer additional user questions using stored file data."""
    file_id = request.form.get('file_id')
    question = request.form.get('question', '').strip()
    
    if not file_id or not question:
        return jsonify({'error': 'Missing file ID or question'}), 400
    
    file_data = get_file_data(file_id)
    if not file_data:
        return jsonify({'error': 'We could not find your uploaded file. Please upload it again.'}), 404
    
    try:

        ai_answer = ask_openai_with_enhanced_context(
            file_data['data_summary'], 
            question, 
            file_data['errors'], 
            file_data['trends'],
            file_data['qa_history']
        )
        
        add_qa_to_history(file_id, question, ai_answer)
        
        return jsonify({
            'answer': ai_answer,
            'question': question,
            'timestamp': datetime.now().strftime('%H:%M:%S')
        })
    
    except Exception as e:
        print(f"Error processing question: {str(e)}")
        return jsonify({'error': f'Error processing question: {str(e)}'}), 500

@app.route('/chat/<file_id>')
def chat_interface(file_id):
    """Render the conversation page for a previously uploaded file."""
    file_data = get_file_data(file_id)
    if not file_data:
        flash('We could not find your uploaded file. Please upload it again.')
        return redirect(url_for('index'))
    
    df = file_data['dataframe']
    display_df = df.head(100) if len(df) > 100 else df
    file_data_html = display_df.to_html(classes='table table-bordered table-striped', 
                                       index=False, 
                                       table_id='data-table')
    
    return render_template('chat.html',
                         file_data=file_data_html,
                         file_info=file_data['file_info'],
                         errors=file_data['errors'],
                         trends=file_data['trends'],
                         file_id=file_id,
                         qa_history=file_data['qa_history'])

@app.errorhandler(413)
def too_large(e):
    """Handle file too large error"""
    flash("The file is too large. Please upload a spreadsheet under 5 GB.")
    return redirect(url_for('index'))


@app.before_request
def cleanup_old_files():
    """Remove file data from memory if it is older than one hour."""
    current_time = datetime.now()
    to_remove = []
    for file_id, data in file_store.items():
        if (current_time - data['timestamp']).seconds > 3600:
            to_remove.append(file_id)
    
    for file_id in to_remove:
        del file_store[file_id]

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
