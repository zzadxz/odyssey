"""Prediction module for loading and running EHR models on patient data, both for clinical predictive tasks and EHR forecasting."""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple, Union

import torch

from odyssey.data.tokenizer import ConceptTokenizer
from odyssey.models.cehr_big_bird.model import BigBirdFinetune, BigBirdPretrain
from odyssey.models.ehr_mamba.model import MambaPretrain

import torch
from typing import Union
from transformers import PreTrainedTokenizer, PreTrainedTokenizerFast


def load_pretrained_model(
    model_type: str,
    tokenizer: Union[PreTrainedTokenizer, PreTrainedTokenizerFast],
    device: torch.device,
    model_path: str
) -> torch.nn.Module:
    """
    Load a pretrained model based on the specified model type and tokenizer.

    This function initializes a model of the specified type, loads its pretrained
    weights from a checkpoint file, and prepares it for inference on the specified device.

    Parameters
    ----------
    model_type : str
        The type of model to load. Currently implements "mamba".
    tokenizer : Union[PreTrainedTokenizer, PreTrainedTokenizerFast]
        The tokenizer associated with the model, used to determine vocabulary size
        and special token IDs.
    device : torch.device
        The device (CPU or GPU) on which to load the model.
    model_path : str
        The file path to the saved model checkpoint.

    Returns
    -------
    torch.nn.Module
        The loaded and prepared model, ready for inference.
    """
    if model_type == "mamba":
        model = MambaPretrain(
            vocab_size=tokenizer.get_vocab_size(),
            embedding_size=768,
            state_size=16,
            num_hidden_layers=32,
            max_seq_length=2048,
            padding_idx=tokenizer.get_pad_token_id(),
            cls_idx=tokenizer.get_class_token_id(),
        )
    else:
        raise ValueError(f"Unsupported model type: {model_type}")

    # Load the pretrained weights
    checkpoint = torch.load(model_path, map_location=device)
    state_dict = checkpoint["state_dict"]
    model.load_state_dict(state_dict)

    # Set the model to evaluation mode and move it to the specified device
    model.eval()
    model.to(device)

    return model


def load_finetuned_model(
    model_path: str,
    tokenizer: ConceptTokenizer,
    pre_model_config: Optional[Dict[str, Any]] = None,
    fine_model_config: Optional[Dict[str, Any]] = None,
    device: Optional[torch.device] = None,
) -> torch.nn.Module:
    """Load a finetuned model from model_path using tokenizer information.

    Return a loaded finetuned model from model_path, using tokenizer information.
    If config arguments are not provided, the default configs built into the
    PyTorch classes are used.

    Parameters
    ----------
    model_path: str
        Path to the finetuned model to load
    tokenizer: ConceptTokenizer
        Loaded tokenizer object
    pre_model_config: Dict[str, Any], optional
        Optional config to override default values of a pretrained model
    fine_model_config: Dict[str, Any], optional
        Optional config to override default values of a finetuned model
    device: torch.device, optional
        CUDA device. By default, GPU is used

    Returns
    -------
    torch.nn.Module
        Finetuned model loaded from model_path

    """
    # Load GPU or CPU device
    if not device:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Create the skeleton of a pretrained and finetuned model
    pretrained_model = BigBirdPretrain(
        vocab_size=tokenizer.get_vocab_size(),
        padding_idx=tokenizer.get_pad_token_id(),
        **(pre_model_config or {}),
    )

    model = BigBirdFinetune(
        pretrained_model=pretrained_model,
        **(fine_model_config or {}),
    )

    # Load the weights using model_path directory
    state_dict = torch.load(model_path, map_location=device)["state_dict"]
    model.load_state_dict(state_dict)
    model.to(device)
    model.eval()

    return model


def predict_patient_outcomes(
    patient: Dict[str, torch.Tensor],
    model: torch.nn.Module,
    device: Optional[torch.device] = None,
) -> Any:
    """Compute model output predictions on given patient data.

    Parameters
    ----------
    patient: Dict[str, torch.Tensor]
        Patient data as a dictionary of tensors
    model: torch.nn.Module
        Model to use for prediction
    device: torch.device, optional
        CUDA device. By default, GPU is used

    Returns
    -------
    Any
        Model output predictions on the given patient data

    """
    # Load GPU or CPU device
    if not device:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Load patient information as a Tuple
    patient_inputs = (
        patient["concept_ids"].to(device),
        patient["type_ids"].to(device),
        patient["time_stamps"].to(device),
        patient["ages"].to(device),
        patient["visit_orders"].to(device),
        patient["visit_segments"].to(device),
    )
    patient_labels = patient["labels"].to(device)
    patient_attention_mask = patient["attention_mask"].to(device)

    # Get model output predictions
    model.to(device)

    return model(
        inputs=patient_inputs,
        attention_mask=patient_attention_mask,
        labels=patient_labels,
    )


def update_patient_sequence_for_next_step(
    patient_data: Dict[str, torch.Tensor],
    pad_start_idx: int,
    predicted_token_ids: List[int],
    num_tokens: int,
    device: torch.device,
) -> Dict[str, torch.Tensor]:
    """
    Update the patient sequence for the next prediction step by incorporating predicted tokens.

    Args:
        patient_data (Dict[str, torch.Tensor]): The original patient data containing different tensors.
        pad_start_idx (int): The index indicating the start of padding tokens in the sequence.
        predicted_token_ids (List[int]): List of predicted token IDs so far.
        num_tokens (int): Number of tokens to predict. Default is 10.
        device (torch.device): The device to run the model on.


    Returns
    -------
        Dict[str, torch.Tensor]: The updated input sequence prepared for the model.
    """
    updated_input = {}
    num_predicted_tokens = len(predicted_token_ids)

    for key, tensor in patient_data.items():
        # Skip keys that are not part of the input sequence
        if key in ("task", "labels", "task_indices"):
            continue

        # Truncate the tensor to exclude padding tokens and predicted tokens
        truncated_tensor = tensor[:pad_start_idx][:-num_tokens]

        # Append predicted tokens to the concept_ids, or zeros for other tensor types
        if key == "concept_ids":
            new_tokens = torch.tensor(
                predicted_token_ids, dtype=torch.long, device=device
            )
            updated_tensor = torch.cat([truncated_tensor, new_tokens])
        else:
            padding_tokens = torch.zeros(
                num_predicted_tokens, dtype=torch.long, device=device
            )
            updated_tensor = torch.cat([truncated_tensor, padding_tokens])

        # Add a batch dimension and move the tensor to the appropriate device
        updated_input[key] = updated_tensor.unsqueeze(0).to(device)

    return updated_input


def predict_next_token(
    model: torch.nn.Module,
    input_sample: Dict[str, torch.Tensor],
    tokenizer: ConceptTokenizer,
) -> Tuple[int, str]:
    """
    Use the model for inference to predict the next token in EHR sequence.

    Args:
        model (torch.nn.Module): The model used for generating predictions.
        input_sample (Dict[str, torch.Tensor]): The input sample prepared for the model.
        tokenizer (ConceptTokenizer): The tokenizer used for EHR data.

    Returns
    -------
        Tuple[int, str]: The predicted token ID and token.
    """
    inputs = (
        input_sample["concept_ids"],
        input_sample["type_ids"],
        input_sample["time_stamps"],
        input_sample["ages"],
        input_sample["visit_orders"],
        input_sample["visit_segments"],
    )

    # Model inference
    output = model(inputs, labels=None, output_hidden_states=False, return_dict=True)

    # Compute probabilities and get the prediction
    probs = torch.softmax(output["logits"][:, -1, :].squeeze(), dim=-1)
    prediction_id = torch.argmax(probs).item()
    prediction_token = tokenizer.id_to_token(prediction_id)

    return prediction_id, prediction_token


def generate_predictions(
    patient_data: Dict[str, Union[torch.Tensor, str]],
    model: torch.nn.Module,
    tokenizer: ConceptTokenizer,
    device: torch.device,
    num_tokens: int = 10,
) -> Tuple[List[int], List[str]]:
    """
    Generate predicted tokens for a patient sequence used in EHR forecasting.

    Args:
        patient_data (Dict[str, torch.Tensor]): A dictionary containing patient data with keys 'concept_ids', 'type_ids',
                                                'time_stamps', 'ages', 'visit_orders', 'visit_segments', and 'labels'.
        model (torch.nn.Module): The model used for generating predictions.
        tokenizer (ConceptTokenizer): The tokenizer used for EHR data.
        device (torch.device): The device to run the model on (e.g., 'cuda' or 'cpu').
        num_tokens (int): Number of tokens to predict. Default is 10.

    Returns
    -------
        Tuple[List[int], List[str]]: A tuple containing two lists:
                                     - predicted_token_ids: List of predicted token IDs.
                                     - predicted_tokens: List of predicted tokens.
    """
    # Prepare model and data for inference
    model.eval()
    model.to(device)
    patient_data = {
        key: value.to(device) if isinstance(value, torch.Tensor) else value
        for key, value in patient_data.items()
    }

    # Determine the index of the first padding token, or use the full length if no padding is present
    if 0 in patient_data["concept_ids"]:
        pad_start_idx = patient_data["concept_ids"].tolist().index(0)
    else:
        pad_start_idx = len(patient_data["concept_ids"])

    # Initialize lists to store the predicted token IDs and their corresponding tokens
    predicted_token_ids = []
    predicted_tokens = []

    for _ in range(num_tokens):
        # Prepare the input sample for the next prediction step by updating the patient sequence
        input_sample = update_patient_sequence_for_next_step(
            patient_data, pad_start_idx, predicted_token_ids, num_tokens, device
        )

        # Generate the next token prediction
        prediction_id, prediction_token = predict_next_token(
            model, input_sample, tokenizer
        )

        # Append the predicted token ID and token to the respective lists
        predicted_token_ids.append(prediction_id)
        predicted_tokens.append(prediction_token)

    return predicted_token_ids, predicted_tokens
