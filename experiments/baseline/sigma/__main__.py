from collections import Counter
import argformat
import argparse
import json
import numpy  as np
import pandas as pd

# Local imports
import sys
sys.path.insert(0, '../ossim/')
from parse        import load_mapping_events, load_mapping_ossim, map_rules
from engine       import RuleEngine
from sigma_engine import SigmaEngine

import sys
sys.path.insert(0, '../../../deepcase/')
from sklearn.metrics import classification_report
from utils import confusion_report
from interpreter.utils import lookup_table

if __name__ == "__main__":
    ########################################################################
    #                           Parse arguments                            #
    ########################################################################

    parser = argparse.ArgumentParser(
        description     = "Compute #alerts when throttled",
        formatter_class = argformat.StructuredFormatter,
    )
    parser.add_argument('file'                                 , help=".csv file containing alerts")
    parser.add_argument('-m', '--max', type=float, default=None, help="maximum number of events to handle")
    parser.add_argument('--offset'   , type=float, default=None, help="number of rows to skip - for train/test")
    args = parser.parse_args()

    # Parse offset if any is given
    if args.offset is not None:
        args.offset = range(1, int(args.offset))
    if args.max is not None:
        args.max = int(args.max)

    ########################################################################
    #                              Load rules                              #
    ########################################################################

    # Save rules
    with open('rules_manual.json', 'r') as infile:
        rules = json.load(infile)

    # Load mapping of events
    mapping_events = load_mapping_events("{}.encoding.json".format(args.file))
    mapping_sigma  = load_mapping_ossim ('sigma_map_threat.csv')

    # Map rules
    rules = map_rules(
        rules          = rules,
        mapping_ossim  = mapping_sigma,
        mapping_events = mapping_events,
    )

    # Save mapped rules
    with open('rules_mapped.json', 'w') as outfile:
        json.dump(rules, outfile, indent='    ')

    threats = pd.read_csv('threat_map.csv', index_col=0, names=['threats'])
    threats = set(threats['threats'].values.tolist())

    all_events  = set()
    all_indices = set()

    for rule in rules:
        for directive in rule['directives']:
            for subrule in directive['rules']:
                for index, event in subrule['events'].items():
                    assert event in threats
                    all_events .add(event)
                    all_indices.add(index)

    ########################################################################
    #                          Create rule engine                          #
    ########################################################################

    # Create rule engine
    engine = SigmaEngine()
    # Fit engine with rules
    engine.fit(rules)

    ########################################################################
    #                        Load and predict data                         #
    ########################################################################

    # Load events
    data = pd.read_csv(args.file, nrows=args.max, skiprows=args.offset)

    # Apply rules to data
    result, alerts = engine.predict(data, verbose=True)

    ########################################################################
    #                             Print result                             #
    ########################################################################

    matched = np.sum(result != -1)
    print("Matched: {}/{} = {:8.4f}%".format(matched, result.shape[0], 100*matched/result.shape[0]))
    print("Alerts: {}".format(alerts))

    # Compute missed alerts - assumes that if an alert is triggered it is always correct
    y_true = np.zeros(data.shape[0], dtype=int)
    y_true[data['impact'].values >   0] = 1
    y_true[data['impact'].values >= 30] = 2
    y_true[data['impact'].values >= 70] = 3
    y_true[data['breach'].values >   0] = 4

    # Set prediction
    y_pred_correct      = np.full(data.shape[0], -1, dtype=int)
    y_pred_conservative = np.full(data.shape[0], -1, dtype=int)
    y_pred_common       = np.full(data.shape[0], -1, dtype=int)

    for event, indices in lookup_table(result):
        if event != -1:
            # Set correct prediction
            y_pred_correct[indices] = y_true[indices]
            # Set conservative prediction
            y_pred_conservative[indices] = y_true[indices].max()
            # Set most common prediction
            y_pred_common[indices] = Counter(y_true[indices]).most_common(1)[0][0]


    ########################################################################
    #                           Print prediction                           #
    ########################################################################

    for y_pred, message in zip([y_pred_correct, y_pred_conservative, y_pred_common], ['correct', 'conservative', 'common']):

        print()
        print("Prediction risk: {}".format(message))
        print("-"*60)

        print(classification_report(
            y_true        = y_true[y_pred != -1],
            y_pred        = y_pred[y_pred != -1],
            digits        = 4,
            labels        = [0, 1, 2, 3, 4],
            target_names  = ['INFO', 'LOW', 'MEDIUM', 'HIGH', 'ATTACK'],
            zero_division = 0,
        ))

        print("-"*60)

        print(confusion_report(
            y_true        = y_true,
            y_pred        = y_pred,
            labels        = [-1, 0, 1, 2, 3, 4],
            target_names  = ['-1', 'INFO', 'LOW', 'MEDIUM', 'HIGH', 'ATTACK'],
        ))

        print("-"*60)

        print("Unable to predict {}/{} = {:.2f}%".format(
            (y_pred == -1).sum(),
            y_pred.shape[0],
            100 * (y_pred == -1).sum() / y_pred.shape[0],
        ))

        for i, risk_level in zip(range(1, 5), ['LOW', 'MEDIUM', 'HIGH', 'ATTACK']):
            print(
                "Missed {:6}: {:{width}}/{:{width}} = {:8.4f}%"
                .format(
                    risk_level,
                    (y_true[y_pred == -1] == i).sum(),
                    (y_true               == i).sum(),
                    100 * (y_true[y_pred == -1] == i).sum() / (y_true == i).sum(),
                    width = 6,
                ))

        print(
            "Missed total : {:{width}}/{:{width}} = {:8.4f}%"
            .format(
                (y_true[y_pred == -1] > 0).sum(),
                (y_true               > 0).sum(),
                100 * (y_true[y_pred == -1] > 0).sum() / (y_true > 0).sum(),
                width = 6,
            ))