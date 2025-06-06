import os
import sys
import pandas as pd
import numpy as np
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from sklearn.metrics import classification_report, confusion_matrix, accuracy_score, f1_score
import matplotlib.pyplot as plt
import seaborn as sns
import time
import logging
import argparse
from datetime import datetime
from huggingface_hub import login
import re
import gc

# Default configurations
DEFAULT_MODEL_NAME = 'deepseek-ai/DeepSeek-R1-Distill-Qwen-7B'
DEFAULT_GPU_ID = 1
DEFAULT_RANDOM_SEED = 42
DEFAULT_MAX_LENGTH = 4096

def parse_arguments():
    """Parse command line arguments with sensible defaults"""
    parser = argparse.ArgumentParser(description='Few-shot learning for regional bias detection with DeepSeek (150 examples)')
    
    parser.add_argument('--examples_path', type=str, 
                        default=os.environ.get('EXAMPLES_PATH', 'data/150_examples_few_shot_classification_dataset.csv'),
                        help='Path to CSV file with 150 few-shot examples')
    parser.add_argument('--test_path', type=str, 
                        default=os.environ.get('TEST_PATH', 'data/annotated_dataset.csv'),
                        help='Path to CSV file with test dataset')
    parser.add_argument('--output_dir', type=str, 
                        default=os.environ.get('OUTPUT_DIR', 'results/deepseek_few_shot_150'),
                        help='Directory to save results')
    parser.add_argument('--cache_dir', type=str, 
                        default=os.environ.get('CACHE_DIR', 'model_cache'),
                        help='Directory for model cache')
    parser.add_argument('--log_dir', type=str, 
                        default=os.environ.get('LOG_DIR', 'logs'),
                        help='Directory for log files')
    parser.add_argument('--model_name', type=str, 
                        default=os.environ.get('MODEL_NAME', DEFAULT_MODEL_NAME),
                        help='Model name or path')
    parser.add_argument('--gpu_id', type=int, 
                        default=int(os.environ.get('GPU_ID', DEFAULT_GPU_ID)),
                        help='GPU ID to use (defaults to GPU 1)')
    parser.add_argument('--hf_token', type=str, 
                        default=os.environ.get('HF_TOKEN', ''),
                        help='HuggingFace token (recommended to use env var instead)')
    parser.add_argument('--random_seed', type=int,
                        default=int(os.environ.get('RANDOM_SEED', DEFAULT_RANDOM_SEED)),
                        help='Random seed for reproducibility')
    parser.add_argument('--test_limit', type=int, default=None,
                        help='Limit number of test examples (for testing)')
    parser.add_argument('--checkpoint_interval', type=int, default=10,
                        help='Interval for saving checkpoints')
    parser.add_argument('--max_length', type=int, 
                        default=int(os.environ.get('MAX_LENGTH', DEFAULT_MAX_LENGTH)),
                        help='Maximum context length for tokenization')
    
    return parser.parse_args()

def create_directory(directory_path, logger=None):
    """Create directory if it doesn't exist"""
    os.makedirs(directory_path, exist_ok=True)
    if logger:
        logger.info(f"Directory created/verified: {directory_path}")

def setup_logging(log_dir, model_name):
    """Set up logging configuration"""
    create_directory(log_dir)
    
    log_file = os.path.join(log_dir, f"{model_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )
    
    return logging.getLogger(__name__)

def clean_text(text):
    """Clean and normalize text for model input"""
    text = str(text).lower()
    text = re.sub(r"http\S+", "", text)  # Remove URLs
    text = re.sub(r"[^a-zA-Z0-9\s]", "", text)  # Remove special characters
    text = re.sub(r"\s+", " ", text).strip()  # Remove extra spaces
    return text

def load_datasets(examples_path, test_path, logger, test_limit=None):
    """Load the example and test datasets"""
    # Check if files exist
    if not os.path.exists(examples_path):
        logger.error(f"Examples file not found: {examples_path}")
        raise FileNotFoundError(f"Examples file not found: {examples_path}")
        
    if not os.path.exists(test_path):
        logger.error(f"Test dataset file not found: {test_path}")
        raise FileNotFoundError(f"Test dataset file not found: {test_path}")
    
    # Load few-shot examples
    logger.info(f"Loading examples from {examples_path}")
    examples_df = pd.read_csv(examples_path)
    
    # Load test dataset
    logger.info(f"Loading test dataset from {test_path}")
    test_df = pd.read_csv(test_path)
    
    # Log dataset info
    logger.info(f"Loaded {len(examples_df)} examples and {len(test_df)} test comments")
    
    # Verify class distribution in examples
    bias_examples = examples_df[examples_df['Level-1'] >= 1]
    non_bias_examples = examples_df[examples_df['Level-1'] == 0]
    logger.info(f"Found {len(bias_examples)} regional bias examples and {len(non_bias_examples)} non-regional bias examples")
    
    # Check if we have the expected number (75 of each)
    expected_count = 75
    if len(bias_examples) != expected_count or len(non_bias_examples) != expected_count:
        logger.warning(f"Expected {expected_count} examples of each class, but found {len(bias_examples)} bias and {len(non_bias_examples)} non-bias examples")
    
    # Ensure there's no overlap between examples and test data
    examples_comments = set(examples_df['Comment'].str.strip())
    test_df = test_df[~test_df['Comment'].str.strip().isin(examples_comments)]
    logger.info(f"After removing overlapping comments, {len(test_df)} test comments remain")
    
    # Clean comments
    logger.info("Cleaning comment text...")
    test_df["Cleaned_Comment"] = test_df["Comment"].apply(clean_text)
    examples_df["Cleaned_Comment"] = examples_df["Comment"].apply(clean_text)
    
    # Apply test limit if specified
    if test_limit is not None and test_limit > 0:
        logger.info(f"Limiting test set to {test_limit} examples")
        test_df = test_df.head(test_limit)
    
    return examples_df, test_df

def create_few_shot_prompt(examples_df, comment, random_seed=42):
    """Create a prompt for few-shot learning with examples and the target comment"""
    # Combine all examples and shuffle
    all_examples = examples_df.copy()
    all_examples = all_examples.sample(frac=1, random_state=random_seed)
    
    # Create the prompt in DeepSeek format
    prompt = "You are an expert in identifying regional biases in comments about Indian states and regions. "
    prompt += "Task: Classify if the comment contains regional bias related to Indian states or regions.\n\n"
    prompt += "Instructions:\n"
    prompt += "- Regional Bias (1): Comments that contain stereotypes, prejudices, or biases about specific Indian states or regions.\n"
    prompt += "- Non-Regional Bias (0): Comments that don't contain regional stereotypes or biases about Indian states.\n\n"
    prompt += "Examples:\n"
    
    for i, row in all_examples.iterrows():
        # Convert Level-1 to binary classification (0 or 1)
        classification = 1 if row['Level-1'] >= 1 else 0
        
        prompt += f"Comment: \"{row['Cleaned_Comment']}\"\n"
        prompt += f"Classification: {classification}\n\n"
    
    prompt += f"Now classify this comment:\n\"{comment}\"\nClassification:"
    
    return prompt

def setup_model(model_name, cache_dir, gpu_id, hf_token, logger):
    """Load model and tokenizer with optimized settings"""
    # Create cache directory
    create_directory(cache_dir, logger)
    
    # Set environment variables for caching
    os.environ["TRANSFORMERS_CACHE"] = cache_dir
    os.environ["HF_HOME"] = cache_dir
    os.environ["HF_DATASETS_CACHE"] = cache_dir
    logger.info(f"Using cache directory: {cache_dir}")
    
    # Set GPU device if available
    if torch.cuda.is_available():
        os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu_id)
        torch.cuda.set_device(0)  # After setting CUDA_VISIBLE_DEVICES, we use device 0
        device = torch.device("cuda:0")
        logger.info(f"Using GPU {gpu_id}: {torch.cuda.get_device_name(0)}")
        logger.info(f"GPU memory: {torch.cuda.get_device_properties(0).total_memory / 1e9:.2f} GB")
    else:
        device = torch.device("cpu")
        logger.info("CUDA not available. Using CPU.")
    
    # Clear GPU cache before loading model
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    
    # Login to HuggingFace if token provided
    if hf_token:
        login(token=hf_token)
        logger.info("Logged in to HuggingFace")
    
    logger.info(f"Loading model: {model_name}")
    start_time = time.time()
    
    try:
        # Configure tokenizer
        tokenizer_kwargs = {
            'trust_remote_code': True,
        }
        
        if hf_token:
            tokenizer_kwargs['token'] = hf_token
        if cache_dir:
            tokenizer_kwargs['cache_dir'] = cache_dir
        
        tokenizer = AutoTokenizer.from_pretrained(model_name, **tokenizer_kwargs)
        
        # Ensure tokenizer has padding token
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        
        # Configure quantization
        quantization_config = BitsAndBytesConfig(
            load_in_8bit=True,
            bnb_8bit_use_double_quant=True,
            bnb_8bit_compute_dtype=torch.float16
        )
        
        # Load model
        model_kwargs = {
            'quantization_config': quantization_config,
            'low_cpu_mem_usage': True,
            'trust_remote_code': True,
        }
        
        if hf_token:
            model_kwargs['token'] = hf_token
        if cache_dir:
            model_kwargs['cache_dir'] = cache_dir
        
        if torch.cuda.is_available():
            model_kwargs['device_map'] = "auto"
        
        model = AutoModelForCausalLM.from_pretrained(model_name, **model_kwargs)
        model.eval()
        
        elapsed_time = time.time() - start_time
        logger.info(f"Model loaded in {elapsed_time:.2f} seconds")
        
        return model, tokenizer, device
        
    except Exception as e:
        logger.error(f"Error loading model: {e}")
        raise

def predict_with_model(model, tokenizer, prompt, device, max_length=4096, max_tokens=10, logger=None):
    """Generate prediction using model"""
    try:
        # Tokenize the prompt with truncation
        inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=max_length).to(device)
        
        # Log token count if in debug mode
        if logger and logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Tokenized prompt length: {inputs['input_ids'].shape[1]} tokens")
        
        # Handle potential missing token IDs
        pad_token_id = tokenizer.pad_token_id
        if pad_token_id is None and tokenizer.eos_token_id is not None:
            pad_token_id = tokenizer.eos_token_id
        
        # Generate response
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=max_tokens,
                temperature=0.1,
                do_sample=False,
                num_beams=1,
                pad_token_id=pad_token_id
            )
        
        # Decode the generated text
        full_output = tokenizer.decode(outputs[0], skip_special_tokens=True)
        
        # Extract the model's response after our prompt
        prompt_length = len(prompt)
        if full_output.startswith(prompt):
            generated_text = full_output[prompt_length:].strip()
        else:
            # If we can't find the exact prompt (tokenization differences)
            generated_text = full_output[-50:].strip()
        
        # Debug logging
        if logger and logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Generated text: {generated_text}")
        
        # Clear tensors
        del inputs, outputs
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        
        # Parse the response to get the classification
        # First check for explicit numbers at the beginning
        if generated_text.startswith("1") or generated_text == "1":
            return 1, full_output
        elif generated_text.startswith("0") or generated_text == "0":
            return 0, full_output
        
        # If not found, look at the last part of the output
        last_part = full_output[-50:].lower()
        
        if "1" in last_part and not "0" in last_part:
            return 1, full_output
        elif "0" in last_part and not "1" in last_part:
            return 0, full_output
        elif "regional bias" in last_part or "bias" in last_part:
            return 1, full_output
        elif "non-regional" in last_part or "no bias" in last_part:
            return 0, full_output
        
        # Default to non-regional bias if unclear
        if logger:
            logger.warning(f"Unclear classification from response: {last_part}")
        return 0, full_output
        
    except Exception as e:
        if logger:
            logger.error(f"Error in prediction: {e}")
        return 0, f"ERROR: {str(e)}"

def batch_predict(model, tokenizer, test_df, examples_df, device, max_length=4096, random_seed=42, 
                 checkpoint_interval=10, output_dir=None, logger=None):
    """Process comments in batches for inference"""
    predictions = []
    raw_outputs = []
    
    # Get test comments
    test_comments = test_df["Cleaned_Comment"].tolist()
    
    # Create checkpoint directory if needed
    if output_dir:
        checkpoint_dir = os.path.join(output_dir, "checkpoints")
        create_directory(checkpoint_dir, logger)
    
    # Process examples
    for i in range(0, len(test_comments)):
        comment = test_comments[i]
        
        try:
            # Generate prompt with all examples
            prompt = create_few_shot_prompt(examples_df, comment, random_seed)
            
            # Check token length
            tokens = tokenizer(prompt, return_tensors="pt", truncation=False)
            input_ids_length = tokens.input_ids.shape[1]
            
            # Use simplified prompt if too long
            if input_ids_length > max_length:
                logger.warning(f"Prompt too long ({input_ids_length} tokens). Using simplified version.")
                
                prompt = "You are an expert in identifying regional biases in comments about Indian states and regions. "
                prompt += "Task: Classify if the comment contains regional bias related to Indian states or regions.\n\n"
                prompt += "Instructions:\n"
                prompt += "- Regional Bias (1): Comments that contain stereotypes, prejudices, or biases about specific Indian states or regions.\n"
                prompt += "- Non-Regional Bias (0): Comments that don't contain regional stereotypes or biases about Indian states.\n\n"
                prompt += "Examples are provided separately. Based on these instructions:\n\n"
                prompt += f"Classify this comment:\n\"{comment}\"\nClassification:"
            
            # Clear tokens
            del tokens
            
            # Get prediction
            prediction, raw_output = predict_with_model(model, tokenizer, prompt, device, max_length, logger=logger)
            
            # Store results
            predictions.append(prediction)
            raw_outputs.append(raw_output)
            
            # Log progress
            if (i + 1) % 10 == 0 or i == 0:
                logger.info(f"Processed example {i+1}/{len(test_comments)}")
                logger.info(f"Decision: {prediction} (0=non-regional, 1=regional)")
            
            # Save checkpoint
            if output_dir and ((i + 1) % checkpoint_interval == 0 or i == len(test_comments) - 1):
                model_short_name = "deepseek_7b_150examples"
                
                checkpoint_df = pd.DataFrame({
                    'Comment': test_df['Comment'].iloc[:i+1].tolist(),
                    'Cleaned_Comment': test_df['Cleaned_Comment'].iloc[:i+1].tolist(),
                    'True_Label': test_df['Level-1'].iloc[:i+1].apply(lambda x: 1 if x >= 1 else 0).tolist(),
                    'Predicted': predictions[:i+1],
                    'Model_Output': [str(output)[:500] for output in raw_outputs[:i+1]]  # Truncate long outputs
                })
                checkpoint_path = os.path.join(output_dir, "checkpoints", f"{model_short_name}_checkpoint_{i+1}.csv")
                checkpoint_df.to_csv(checkpoint_path, index=False)
                logger.info(f"Saved checkpoint at {checkpoint_path}")
        
        except Exception as e:
            logger.error(f"Error processing comment {i+1}: {e}")
            predictions.append(0)  # Default to non-regional bias
            raw_outputs.append(f"ERROR: {str(e)}")
        
        # Clear cache
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            gc.collect()
    
    return predictions, raw_outputs

def save_results(test_df, predictions, raw_outputs, output_dir, logger, model_name="deepseek_7b_150examples"):
    """Save prediction results and evaluation metrics"""
    # Get true labels
    true_labels = test_df['Level-1'].apply(lambda x: 1 if x >= 1 else 0).tolist()
    
    # Create visualization directory
    viz_dir = os.path.join(output_dir, "visualizations")
    create_directory(viz_dir, logger)
    
    # Save predictions with raw outputs
    results_df = test_df.copy()
    results_df['Predicted'] = predictions
    
    # Truncate raw outputs
    truncated_outputs = [str(output)[:500] for output in raw_outputs]
    results_df['Model_Output'] = truncated_outputs
    
    # Create output paths
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    predictions_path = os.path.join(output_dir, f"{model_name}_predictions_{timestamp}.csv")
    report_path = os.path.join(output_dir, f"{model_name}_report_{timestamp}.txt")
    matrix_path = os.path.join(viz_dir, f"{model_name}_confusion_matrix_{timestamp}.png")
    summary_path = os.path.join(viz_dir, f"{model_name}_results_summary_{timestamp}.png")
    
    # Save predictions CSV
    results_df.to_csv(predictions_path, index=False)
    logger.info(f"Predictions saved to {predictions_path}")
    
    # Generate classification report
    report = classification_report(true_labels, predictions)
    with open(report_path, 'w') as f:
        f.write(f"Classification Report for {model_name}\n\n")
        f.write(f"Timestamp: {timestamp}\n\n")
        f.write(report)
    logger.info(f"Classification report saved to {report_path}")
    
    # Calculate metrics
    accuracy = accuracy_score(true_labels, predictions)
    f1 = f1_score(true_labels, predictions)
    
    # Log metrics
    logger.info(f"Accuracy: {accuracy:.4f}")
    logger.info(f"F1 Score: {f1:.4f}")
    
    # Generate confusion matrix
    cm = confusion_matrix(true_labels, predictions)
    plt.figure(figsize=(10, 8))
    sns.heatmap(cm, annot=True, fmt='d', cmap='Blues', 
                xticklabels=['Non-Regional Bias', 'Regional Bias'],
                yticklabels=['Non-Regional Bias', 'Regional Bias'])
    plt.xlabel('Predicted')
    plt.ylabel('True')
    plt.title(f'Confusion Matrix - {model_name}')
    plt.tight_layout()
    plt.savefig(matrix_path)
    logger.info(f"Confusion matrix saved to {matrix_path}")
    
    # Create results visualization
    plt.figure(figsize=(12, 8))
    
    # 2x2 grid of subplots
    plt.subplot(2, 2, 1)
    # Confusion matrix
    sns.heatmap(cm, annot=True, fmt='d', cmap='Blues',
                xticklabels=['Non-Regional', 'Regional'],
                yticklabels=['Non-Regional', 'Regional'])
    plt.title('Confusion Matrix')
    plt.xlabel('Predicted')
    plt.ylabel('True')
    
    # Class distribution
    plt.subplot(2, 2, 2)
    class_counts = pd.Series(true_labels).value_counts().sort_index()
    plt.bar(['Non-Regional', 'Regional'], [class_counts.get(0, 0), class_counts.get(1, 0)], 
            color=['#1f77b4', '#ff7f0e'])
    plt.title('Test Set Class Distribution')
    plt.ylabel('Number of Samples')
    
    # Add value labels
    plt.text(0, class_counts.get(0, 0) + 5, str(class_counts.get(0, 0)), ha='center')
    plt.text(1, class_counts.get(1, 0) + 5, str(class_counts.get(1, 0)), ha='center')
    
    # Prediction distribution
    plt.subplot(2, 2, 3)
    pred_counts = pd.Series(predictions).value_counts().sort_index()
    plt.bar(['Non-Regional', 'Regional'], [pred_counts.get(0, 0), pred_counts.get(1, 0)], 
            color=['#2ca02c', '#d62728'])
    plt.title('Model Predictions')
    plt.ylabel('Number of Samples')
    
    # Add value labels
    plt.text(0, pred_counts.get(0, 0) + 5, str(pred_counts.get(0, 0)), ha='center')
    plt.text(1, pred_counts.get(1, 0) + 5, str(pred_counts.get(1, 0)), ha='center')
    
    # Accuracy and F1
    plt.subplot(2, 2, 4)
    plt.bar(['Accuracy', 'F1 Score'], [accuracy, f1], color=['#9467bd', '#8c564b'])
    plt.ylim(0, 1.0)
    plt.title('Model Performance')
    plt.text(0, accuracy + 0.05, f"{accuracy:.4f}", ha='center')
    plt.text(1, f1 + 0.05, f"{f1:.4f}", ha='center')
    
    plt.tight_layout()
    plt.savefig(summary_path)
    logger.info(f"Results summary visualization saved to {summary_path}")
    
    return accuracy, f1

def main():
    """Main execution function"""
    # Parse arguments
    args = parse_arguments()
    
    # Create required directories
    for directory in [args.output_dir, args.cache_dir, args.log_dir]:
        create_directory(directory)
    
    # Set up model name and logging
    model_short_name = "deepseek_7b_150examples"
    logger = setup_logging(args.log_dir, model_short_name)
    
    # Log arguments (except token)
    logger.info("Arguments:")
    for arg, value in vars(args).items():
        if arg == 'hf_token':
            logger.info(f"  {arg}: {'*' * 8 if value else 'Not provided'}")
        else:
            logger.info(f"  {arg}: {value}")
    
    # Set random seed
    np.random.seed(args.random_seed)
    torch.manual_seed(args.random_seed)
    logger.info(f"Random seed set to {args.random_seed}")
    
    # Start timing
    start_time = time.time()
    
    try:
        # Load datasets
        examples_df, test_df = load_datasets(
            args.examples_path, args.test_path, logger, args.test_limit
        )
        
        # Set up model and tokenizer
        model, tokenizer, device = setup_model(
            args.model_name, args.cache_dir, args.gpu_id, args.hf_token, logger
        )
        
        # Run inference
        logger.info(f"Processing {len(test_df)} comments with {len(examples_df)} few-shot examples...")
        
        predictions, raw_outputs = batch_predict(
            model, tokenizer, test_df, examples_df, device,
            max_length=args.max_length,
            random_seed=args.random_seed,
            checkpoint_interval=args.checkpoint_interval,
            output_dir=args.output_dir,
            logger=logger
        )
        
        # Save results
        accuracy, f1 = save_results(
            test_df, predictions, raw_outputs, args.output_dir, logger, 
            model_name=model_short_name
        )
        
        # Log execution time
        end_time = time.time()
        elapsed_hours = (end_time - start_time) / 3600
        logger.info(f"Total execution time: {elapsed_hours:.2f} hours")
        
        # Final summary
        logger.info("===== Final Summary =====")
        logger.info(f"Model: {args.model_name}")
        logger.info(f"Test set size: {len(test_df)}")
        logger.info(f"Few-shot examples: {len(examples_df)}")
        logger.info(f"Accuracy: {accuracy:.4f}")
        logger.info(f"F1 Score: {f1:.4f}")
    
    except Exception as e:
        logger.error(f"Error in main execution: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
