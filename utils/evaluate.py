import os, sys, pdb
import random
import json
import torch
import numpy as np
import time as tm
import pandas as pd

from torch.utils.data import RandomSampler, SequentialSampler
from torch.utils.data import DataLoader
from collections import defaultdict, OrderedDict, Counter
from sklearn.metrics import accuracy_score

from components.systems import Application
from utils.help import prepare_inputs

def qualify(args, ids, tokenizer, target_maps, scores, targets):
  history_ids, context_ids = ids
  action_mapper, value_mapper = target_maps
  num_values = len(value_mapper)
  pad_id = tokenizer.convert_tokens_to_ids(tokenizer.pad_token)

  action_score, value_score = scores
  action_target, value_target = targets
  top_action_ids = np.argmax(action_score.detach().cpu().numpy(), axis=1)
  top_value_ids = np.argmax(value_score.detach().cpu().numpy(), axis=1)
   
  for index, (history, context) in enumerate(zip(history_ids, context_ids)):
    stripped_history = [x for x in history if x != pad_id]
    history_tokens = tokenizer.convert_ids_to_tokens(stripped_history)
    history_symbols = ' '.join(history_tokens).replace(' ##', '')
    history_text = history_symbols.replace('Ġ', '').replace('</s>', '//').replace(' âĢ Ļ', '\'')
    action_pred = action_mapper[top_action_ids[index]]
    action_actual = action_mapper[action_target[index].cpu()]
    
    if args.filter and (action_pred == action_actual):
      print('--- Skipping since model is correct ---')
      continue

    context_tokens = tokenizer.convert_ids_to_tokens(context)
    tvii = top_value_ids[index]
    if tvii >= num_values:
      tvii -= num_values
      value_pred = context_tokens[tvii]
    else:
      value_pred = value_mapper[tvii]

    vtic = value_target[index].cpu()  
    if vtic >= num_values:
      vtic -= num_values
      value_actual = context_tokens[vtic]
    else:
      value_actual = value_mapper[vtic]
    print(index, history_text)
    print(f"Predicted Action: {action_pred}, Actual: {action_actual}")
    print(f"Predicted Value: {value_pred}, Actual: {value_actual}")

  pdb.set_trace()  

def interact(args, model, dataset):
  app = Application(args, model, dataset)
  print(" ")
  print("Connecting you with an agent ...")
  tm.sleep(2)

  while True:
    end_conversation = False
    turn_count = 0
    so_far = []

    scene, prompt = app.sample_scenario()
    print(prompt)
    for key, val in scene.items():
      print(f'{key}: {val}')     
    print(" ")

    while not end_conversation:

      if turn_count == 0 and random.randint(0, 2) > 0:
        customer = ''  # agent talks first
        text = customer
      else:
        customer = input("Customer: ")

        if customer.lower() == 'skip':
          print("Sampling new scenario ...")
          end_conversation = True
          break
        else:
          cleaned = customer.lower().rstrip()
          so_far.append(cleaned)
          delex = app.delexicalize_text(scene, so_far)
          text = f' [SEP] '.join(delex)

      input_ids, segment_ids, input_mask = app.processor.embed_utterance(text)
      full_history = {'input_ids': input_ids, 'token_type_ids': segment_ids, 'attention_mask': input_mask}

      action_tokens = app.tokenizer.tokenize("pull-up-account")
      filtered = []
      for utterance in so_far:
        context_tokens = app.tokenizer.tokenize(utterance)
        for tok in context_tokens:
          if tok in filtered:  continue       # find uniques this way to preserve order
          if len(tok) > 2:
            filtered.append(tok)            # remove punctuation and special tokens
      effective_max = 100 - (len(action_tokens) + 3)   # three special tokens will be added
      context_tokens = action_tokens + ['[SEP]'] + filtered[-effective_max:]
      context_emb = app.processor.convert_context_tokens(context_tokens)
      context_params = {'token_type_ids': context_emb['segment_ids'], 
            'input_ids': context_emb['token_ids'], 'attention_mask': context_emb['mask_ids']}

      for parameter, value in full_history.items():
        tensor_value = torch.tensor(value, dtype=torch.long, device=app.device).unsqueeze(0)
        full_history[parameter] = tensor_value
      for parameter, value in context_params.items():
        tensor_value = torch.tensor(value, dtype=torch.long, device=app.device).unsqueeze(0)
        context_params[parameter] = tensor_value

      model.eval()           # put into eval mode rather than train mode
      with torch.no_grad():
        predictions = model(full_history, context_params)
      preds = [pred.detach().cpu().numpy().squeeze() for pred in predictions]
      #    55          3              30           225         100
      intent_pred, nextstep_pred, action_pred, value_pred, utterance_pred = preds
      top_nextstep = np.argmax(nextstep_pred)  # no axis needed since all preds are vectors

      print("turn count", turn_count)
      if turn_count > 14:
        print("You have reached the max turn count.")
        end_conversation = True
      elif top_nextstep == 0:  
        top_utterance = np.argmax(utterance_pred)  # squeeze leaves (100,)
        top_index = model.candidate_indexes[top_utterance]
        response = model.utt_texts[top_index]
        print("Agent:", response)
        so_far.append(response)
      elif top_nextstep == 1:
        response = app.take_action(intent_pred, action_pred, value_pred, context_tokens)
        print(f"         {response}")
      else:
        end_conversation = True
      turn_count += 1

    cont = input(f"Conversation ended. Enter 'yes' to continue: ")
    if cont.lower().startswith('y'):
      print("-----------------------------------")
      continue
    else:
      break

  print("Thank you for contacting AcmeBrands customer service.  Have a nice day!")

def accuracy_report(predictions, labels):
  preds = np.argmax(predictions, axis=1)
  acc = accuracy_score(labels, preds)
  full_result = {'Accuracy': round(acc, 4)}

  return full_result, 'Accuracy'

def ranking_report(predictions, labels, use_match=False):
  full_result = {}
  utt_match = []

  for rank in [1,5,10]:
    level = -rank   # select the top 5 rather than bottom 5
    num_correct, num_possible = 0, 0
    # vectorized version possible, but a lot less readable
    for pred, label in zip(predictions, labels):
      top_k_indexes = np.argpartition(pred, kth=level)[level:]
      if label in top_k_indexes:
        num_correct += 1
        if rank == 1:
          utt_match.append(True)
      else:
        if rank == 1:
          utt_match.append(False)

      if label >= 0:    # -1 means the turn was take-action or end-of-convo
        num_possible += 1

    rank_name = f'Recall_at_{rank}'
    full_result[rank_name] = num_correct / num_possible

  if use_match:
    return full_result, utt_match
  else:
    return full_result, 'Recall_at_5'

def aa_with_values_report(predictions, labels):
  action_preds, value_preds = predictions
  action_labels, value_labels = labels

  size = len(action_preds)
  assert(size == len(value_labels))

  top_action_preds = np.argmax(action_preds, axis=1)
  action_match = action_labels == top_action_preds   # array of booleans
  action_acc = sum(action_match) / float(size) 

  top_value_preds = np.argmax(value_preds, axis=1)
  value_match = value_labels == top_value_preds
  value_acc = sum(value_match) / float(size) 

  joint_match = action_match & value_match
  joint_acc = sum(joint_match) / float(size) 

  full_result = {'Action_Accuracy': round(action_acc, 4),
          'Value_Accuracy': round(value_acc, 4),
          'Joint_Accuracy': round(joint_acc, 4),}

  return full_result, 'Joint_Accuracy'

def aa_with_values_breakdown(predictions, targets, target_maps, report):
  action_preds, value_preds = predictions
  action_targets, value_targets = targets
  action_mapper, value_mapper = target_maps

  action_preds = np.argmax(action_preds, axis=1)
  value_preds = np.argmax(value_preds, axis=1)

  full_report = report
  action_report = breakdown_report(action_preds, action_targets, action_mapper)
  full_report.update(action_report)
  value_report = breakdown_report(value_preds, value_targets, value_mapper, 'value')
  full_report.update(value_report)

  return full_report

def breakdown_report(predictions, targets, mapper, task='action'):
  tracker = defaultdict(Counter)
  num_items = len(mapper)

  for pred, target in zip(predictions, targets):
    if target < 0:
      continue
    elif task == 'value' and target >= num_items:
      label = 'copy'
    else:
      label = mapper[target]
    
    if pred == target:
      tracker[label]['right'] += 1
    else:
      tracker[label]['wrong'] += 1
      
  for label, result in tracker.items():
    total = result['right'] + result['wrong']
    tracker[label]['total'] = total
    ratio = round((result['right'] / float(total)) * 100, 3)
    tracker[label]['ratio'] = ratio

  just_ratios = [(label, result['ratio']) for label, result in tracker.items()]
  just_ratios.sort(key=lambda x: x[1], reverse=True)

  breakdown = {}
  upper_limit = 2 if task == 'nextstep' else 5
  lower_limit = len(just_ratios) - upper_limit
  for idx, (label, _) in enumerate(just_ratios):
    if idx < upper_limit or idx >= lower_limit:
      key = f'{task}_0{idx+1}_{label}' if idx < 9 else f'{task}_{idx+1}_{label}'
      right, total, ratio = tracker[label]['right'], tracker[label]['total'], tracker[label]['ratio']
      breakdown[key] = f"{right} out of {total} correct ({ratio}%)"
  
  return breakdown

def task_completion_breakdown(preds, labels, maps, report):
  pred_dict = {'intent': preds[0], 'nextstep': preds[1], 'action': preds[2], 'value': preds[3] }
  label_dict = {'intent': labels[0], 'nextstep': labels[1], 'action': labels[2], 'value': labels[3] }
  mappers = {'intent': maps[2], 'nextstep': maps[3], 'action': maps[0], 'value': maps[1] }
  
  for task in ['intent', 'nextstep', 'action', 'value']:
    top_preds = np.argmax(pred_dict[task], axis=1)
    task_report = breakdown_report(top_preds, label_dict[task], mappers[task], task)
    report.update(task_report)

  return report

def cascaded_evaluation_report(predictions, labels, ci_and_tc, kb_labels=None):
  """ Calculated in the form of cascaded evaluation
  where each agent example or utterance a scored example"""
  intent_pred, nextstep_pred, action_pred, value_pred, utterance_rank = predictions
  intent_label, nextstep_label, action_label, value_label, utterance_label = labels
  convo_ids = ci_and_tc[0].detach().cpu().numpy()
  turn_counts = ci_and_tc[1].detach().cpu().numpy()

  if kb_labels is None:
    use_kb = False
  else:
    use_kb = True
    intent_list = kb_labels['intent']
    action_list = kb_labels['action']

  num_turns = len(nextstep_pred)
  assert(num_turns == len(convo_ids))

  top_intent_preds = np.argmax(intent_pred, axis=1)
  intent_match = intent_label == top_intent_preds   # array of booleans
  intent_acc = sum(intent_match) / float(num_turns) 

  top_nextstep_preds = np.argmax(nextstep_pred, axis=1)
  nextstep_match = nextstep_label == top_nextstep_preds   # array of booleans
  nextstep_acc = sum(nextstep_match) / float(num_turns) 

  if use_kb:
    intent_masks = []
    for top_intent in top_intent_preds:
      intent_name = intent_list[top_intent]
      # each intent mask should be size of 30 long
      intent_mask = intent_mask_map[intent_name]
      intent_masks.append(intent_mask)
    # now, all non valid actions should go to zero
    action_pred *= np.array(intent_masks)

  top_action_preds = np.argmax(action_pred, axis=1)
  action_match = action_label == top_action_preds   # array of booleans
  num_turns_include_action = sum(action_label >= 0)
  action_acc = sum(action_match) / float(num_turns_include_action) 

  if use_kb:
    action_masks = []
    for top_action in top_action_preds:
      action_name = action_list[top_action]
      # each action mask should be size of 223 long
      action_mask = action_mask_map[action_name]
      action_masks.append(action_mask)
    # now, all non valid values should go to zero
    value_pred *= np.array(action_masks)

  top_value_preds = np.argmax(value_pred, axis=1)
  value_match = value_label == top_value_preds
  num_turns_include_value = sum(value_label >= 0)
  value_acc = sum(value_match) / float(num_turns_include_value) 

  joint_match = action_match & value_match
  joint_acc = sum(joint_match) / float(num_turns_include_action) 

  recall, utt_match = {}, []
  for rank in [1,5,10]:
    level = -rank   # select the top 5 rather than bottom 5
    num_correct, num_possible = 0, 0
    for pred, label in zip(utterance_rank, utterance_label):
      top_k_indexes = np.argpartition(pred, kth=level)[level:]
      if label in top_k_indexes:
        num_correct += 1
        if rank == 1:
          utt_match.append(True)
      else:
        if rank == 1:
          utt_match.append(False)

      if label >= 0:
        num_possible += 1
    recall[str(rank)] = num_correct / num_possible 

  # group by convo_ids
  unique_convo_ids = list(set(convo_ids))
  conversations = {}
  for uci in unique_convo_ids:
    turns, correctness = [], []
    row_id = 0
    for convo_id, turn_count in zip(convo_ids, turn_counts):
      if convo_id == uci:
        turns.append(turn_count)

        correct = False
        intent_right = intent_match[row_id]
        nextstep_right = nextstep_match[row_id]

        if nextstep_label[row_id] == 0:
          if intent_right and nextstep_right and utt_match[row_id]:
            correct = True
        elif nextstep_label[row_id] == 1:
          if intent_right and nextstep_right and joint_match[row_id]:
            correct = True
        elif nextstep_label[row_id] == 2:
          if intent_right and nextstep_right:
            correct = True

        correctness.append(correct)
      row_id += 1

    # sort by turn_counts
    ordered = [cor for _, cor in sorted( zip(turns,correctness), key=lambda tc: tc[0] )]
    conversations[uci] = ordered

  # count how many correct
  turn_score, turn_correct = 0, 0
  for convo_id, convo_correctness in conversations.items():
    convo_length = len(convo_correctness)
    # we use turn_id rather than the true turn_count since turn counts will skip numbers
    # when looping through the conversation due to skipping over customer utterances
    for turn_id in range(convo_length):
      num_remaining = convo_length - turn_id
      
      num_correct = 0
      # count up how many were predicted correctly
      while turn_id < convo_length and convo_correctness[turn_id]:
        num_correct += 1
        turn_id += 1

      if num_correct > 0:
        turn_correct += 1
      # normalize by the number of turns remaining
      turn_score += num_correct / num_remaining

  # normalize by total number of turns possible
  turn_acc = turn_correct / float(num_turns)
  final_score = turn_score / float(num_turns)

  full_result = {'Intent_Accuracy': round(intent_acc, 4),
         'Nextstep_Accuracy': round(nextstep_acc, 4),
           'Action_Accuracy': round(action_acc, 4),
          'Value_Accuracy': round(value_acc, 4),
          'Joint_Accuracy': round(joint_acc, 4),
             'Recall_at_1': round(recall['1'], 4),
             'Recall_at_5': round(recall['5'], 4),
            'Recall_at_10': round(recall['10'], 4),
           'Turn_Accuracy': round(turn_acc, 4),
           'Cascading_Score': round(final_score, 4) }

  return full_result, 'Cascading_Score'

def task_completion_report(predictions, labels, kb_labels=None):
  intent_pred, nextstep_pred, action_pred, value_pred, utterance_rank = predictions
  intent_label, nextstep_label, action_label, value_label, utterance_label = labels
  num_turns = len(nextstep_pred)

  if kb_labels is None:
    use_kb = False
  else:
    use_kb = True
    intent_list = kb_labels['intent']
    action_list = kb_labels['action']

  top_intent_preds = np.argmax(intent_pred, axis=1)
  intent_match = intent_label == top_intent_preds   # array of booleans
  intent_acc = sum(intent_match) / float(num_turns) 

  top_nextstep_preds = np.argmax(nextstep_pred, axis=1)
  nextstep_match = nextstep_label == top_nextstep_preds   # array of booleans
  nextstep_acc = sum(nextstep_match) / float(num_turns) 

  if use_kb:
    intent_masks = []
    for top_intent in top_intent_preds:
      intent_name = intent_list[top_intent]
      # each intent mask should be size of 30 long
      intent_mask = intent_mask_map[intent_name]
      intent_masks.append(intent_mask)
    # now, all non valid actions should go to zero
    action_pred *= np.array(intent_masks)

  top_action_preds = np.argmax(action_pred, axis=1)
  action_match = action_label == top_action_preds   # array of booleans
  num_turns_include_action = sum(action_label >= 0) 
  action_acc = sum(action_match) / float(num_turns_include_action) 

  if use_kb:
    action_masks = []
    for top_action in top_action_preds:
      action_name = action_list[top_action]
      # each action mask should be size of 223 long
      action_mask = action_mask_map[action_name]
      action_masks.append(action_mask)
    # now, all non valid values should go to zero
    value_pred *= np.array(action_masks)

  top_value_preds = np.argmax(value_pred, axis=1)
  value_match = value_label == top_value_preds
  num_turns_include_value = sum(value_label >= 0)
  value_acc = sum(value_match) / float(num_turns_include_value) 

  joint_match = action_match & value_match
  joint_acc = sum(joint_match) / float(num_turns_include_action) 

  recall, utt_match = ranking_report(utterance_rank, utterance_label, use_match=True)

  assert(num_turns == len(value_label))
  assert(len(intent_pred) == len(nextstep_label))
  assert(len(utt_match) == num_turns)    
  assert(len(action_match) == len(top_value_preds))  

  turn_correct = 0
  for turn in range(num_turns):
    if intent_match[turn] and nextstep_match[turn]:
      pass
    else:
      continue

    if nextstep_label[turn] == 0 and utt_match[turn]:
      turn_correct += 1
    elif nextstep_label[turn] == 1 and joint_match[turn]:
      turn_correct += 1
    elif nextstep_label[turn] == 2:      # end_conversation
      turn_correct += 1
  turn_acc = turn_correct / float(num_turns)

  full_result = {'Intent_Accuracy': round(intent_acc, 4),
         'Nextstep_Accuracy': round(nextstep_acc, 4),
           'Action_Accuracy': round(action_acc, 4),
          'Value_Accuracy': round(value_acc, 4),
          'Joint_Accuracy': round(joint_acc, 4),
             'Recall_at_1': round(recall['Recall_at_1'], 4),
             'Recall_at_5': round(recall['Recall_at_5'], 4),
            'Recall_at_10': round(recall['Recall_at_10'], 4),
           'Turn_Accuracy': round(turn_acc, 4) }

  return full_result, 'Turn_Accuracy' 

def aa_limited_values_report(predictions, labels, limit_utils):
  mask_mapping, action_list = limit_utils
  action_preds, value_preds = predictions
  action_labels, value_labels = labels

  size = len(action_preds)
  assert(size == len(value_labels))

  top_action_preds = np.argmax(action_preds, axis=1)
  action_match = action_labels == top_action_preds   # array of booleans
  action_acc = sum(action_match) / float(size) 

  masks = []
  for top_action in top_action_preds:
    action_name = action_list[top_action]
    # each action mask should be size of 223 long
    action_mask = mask_mapping[action_name]
    masks.append(action_mask)

  value_masks = np.array(masks)
  # now, all non valid actions should go to zero
  value_preds *= value_masks

  top_value_preds = np.argmax(value_preds, axis=1)
  value_match = value_labels == top_value_preds
  value_acc = sum(value_match) / float(size) 

  joint_match = action_match & value_match
  joint_acc = sum(joint_match) / float(size) 

  full_result = {'Action_Accuracy': round(action_acc, 4),
          'Value_Accuracy': round(value_acc, 4),
          'Joint_Accuracy': round(joint_acc, 4),}

  return full_result, 'Joint_Accuracy'

def quantify(args, preds_and_labels, tools=None):
  predictions, labels = preds_and_labels
  assert len(predictions) == len(labels)

  if args.task == 'utterance':
    predictions = predictions.detach().cpu().numpy()
    labels = labels.detach().cpu().numpy()
    report, res_name = ranking_report(predictions, labels)

  elif args.task == 'aawv':
    predictions = [pred.detach().cpu().numpy() for pred in predictions]
    labels = [label.detach().cpu().numpy() for label in labels]
    # return aa_limited_values_report(predictions, labels, limit_utils)
    report, res_name = aa_with_values_report(predictions, labels)
    if args.breakdown:
      target_maps = tools['target_maps']
      report = aa_with_values_breakdown(predictions, labels, target_maps, report)

  elif args.task in ['tcom', 'tcwi', 'remove']:
    predictions = [pred.detach().cpu().numpy() for pred in predictions]
    labels = [label.detach().cpu().numpy() for label in labels]
    kb_labels = tools['kb_labels'] if args.use_kb else None

    if args.cascade:
      ci_and_tc = tools['ci_and_tc']
      result = cascaded_evaluation_report(predictions, labels, ci_and_tc, kb_labels)
      report, res_name = result
    else:
      report, res_name = task_completion_report(predictions, labels, kb_labels)
    if args.breakdown:
      target_maps = tools['target_maps']
      report = task_completion_breakdown(predictions, labels, target_maps, report)
    elif args.do_eval:
      pass
    else:
      del report['Intent_Accuracy']
      del report['Value_Accuracy']
      del report['Recall_at_5']
      del report['Recall_at_10']

  else:
    predictions = predictions.detach().cpu().numpy()
    labels = labels.detach().cpu().numpy()
    report, res_name = accuracy_report(predictions, labels)

  return report, res_name

if __name__ == '__main__':
  class MyModel():
    def __init__(self):
      self.utt_vectors = []
      self.utt_texts = []

  args = {}
  run_interaction(args, MyModel())
  # allow user to speak multiple turns in a row
  # group together actions that have multiple values