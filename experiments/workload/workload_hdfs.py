from collections     import Counter
from datetime        import datetime, timedelta
from sklearn.metrics import classification_report, homogeneity_score
import argformat
import argparse
import logging
import numpy as np
import sys
import torch
sys.path.insert(1, '../../deepseq/')

from deepseq           import DeepSeq
from context_builder   import ContextBuilder
from interpreter       import Interpreter
from preprocessing     import PreprocessLoader, SimpleLoader
from utils             import box, confusion_report, header
from interpreter.utils import lookup_table

if __name__ == "__main__":
    # Set logging
    logging.basicConfig(level=logging.DEBUG, filename='logging.log')

    ########################################################################
    #                           Parse arguments                            #
    ########################################################################
    # Parse arguments
    parser = argparse.ArgumentParser(
        prog            = "deepseq.py",
        description     = "DeepSeq: providing meta-level contextual analysis of security alerts",
        formatter_class = argformat.StructuredFormatter,
    )

    # Input
    group_input = parser.add_argument_group("Input")
    group_input.add_argument('file'      ,                            help="read preprocessed input     file")
    group_input.add_argument('test'      , nargs='?' ,                help="read preprocessed test      file")
    group_input.add_argument('malicious' , nargs='?' ,                help="read preprocessed malicious file")
    group_input.add_argument('--offset'  , type=float, default=0    , help="offset for items to load")
    group_input.add_argument('--time'    , type=float, default=86400, help="max time length of input sequence")
    group_input.add_argument('--all'     , dest='all'     , action='store_true', help="perform experiment on all data")
    group_input.add_argument('--breach'  , dest='breach'  , action='store_true', help="read breaches")
    group_input.add_argument('--incident', dest='incident', action='store_true', help="read incidents")
    group_input.add_argument('--ignore'  , dest='ignore'  , action='store_true', help="ignore incident and breach info")

    # ContextBuilder parameters
    context_builder = parser.add_argument_group("ContextBuilder")
    context_builder.add_argument('-f', '--features'      , type=int  , default=280,          help="maximum number of expected features")
    context_builder.add_argument('-i', '--dim-input'     , type=int  , default=10,           help="length of input sequence")
    context_builder.add_argument('-o', '--dim-output'    , type=int  , default=1,            help="length of output sequence")
    context_builder.add_argument('-m', '--max-sequences' , type=float, default=float('inf'), help="maximum number of sequences ro read from input")
    context_builder.add_argument('-n', '--max-events'    , type=float, default=float('inf'), help="maximum number of events to read from input")
    context_builder.add_argument('-c', '--complexity'    , type=int  , default=128,          help="complexity of the model")

    # Training
    group_training = parser.add_argument_group("ContextBuilder training")
    group_training.add_argument('-b', '--batch-size', type=int, default=128   , help="batch size")
    group_training.add_argument('-d', '--device'    , type=str, default='auto', help="train using given device (cpu|cuda|auto)")
    group_training.add_argument('-e', '--epochs'    , type=int, default=10    , help="number of epochs to train with")
    group_training.add_argument('-r', '--random'    , action='store_true'     , help="train with random selection")
    group_training.add_argument('-s', '--silent', dest='verbose', action='store_false', help="supress printing progress")
    group_training.add_argument('--train', type=float, default=0.5, help="training samples to use (or ratio of if 0 <= TRAIN <= 1)")

    # Interpreter parameters
    interpreter = parser.add_argument_group("Interpreter")
    interpreter.add_argument('--epsilon'    , type=float, default=0.1, help="interpreter epsilon     for clustering")
    interpreter.add_argument('--min-samples', type=int,   default=5,   help="interpreter min_samples for clustering")
    interpreter.add_argument('--threshold'  , type=float, default=0.2, help="interpreter confidence threshold for fingerprinting")

    # Store/load model
    group_model = parser.add_argument_group("Model I/O parameters")
    group_model.add_argument('--load-context'    , help="load context builder from LOAD file")
    group_model.add_argument('--load-interpreter', help="load interpreter     from LOAD file")
    group_model.add_argument('--save-context'    , help="save context builder to   SAVE file")
    group_model.add_argument('--save-interpreter', help="save interpreter     to   SAVE file")

    # Parse arguments
    args = parser.parse_args()

    ########################################################################
    #                              Set device                              #
    ########################################################################

    # Set device if necessary
    if args.device is None or args.device == 'auto':
        args.device = 'cuda' if torch.cuda.is_available() else 'cpu'

    ########################################################################
    #                              Load data                               #
    ########################################################################

    # Load data - HDFS
    if args.test:
        loader = SimpleLoader()
        X_train, y_train = loader.load(args.file, args.dim_input, args.dim_output, args.max_events)
        X_test , y_test  = loader.load(args.test, args.dim_input, args.dim_output, args.max_events)
        # Cast to device
        X_train = X_train.to(args.device)
        y_train = y_train.to(args.device)
        X_test  = X_test .to(args.device)
        y_test  = y_test .to(args.device)
        encodings = dict()

        X_impact_train = torch.zeros(X_train.shape, dtype=torch.long)
        y_impact_train = torch.zeros(y_train.shape, dtype=torch.long)
        X_impact_test  = torch.zeros(X_test .shape, dtype=torch.long)
        y_impact_test  = torch.zeros(y_test .shape, dtype=torch.long)

        if args.malicious:
            X_test_m, y_test_m = loader.load(args.malicious, args.dim_input, args.dim_output, args.max_events)
            X_test_m = X_test_m.to(args.device)
            y_test_m = y_test_m.to(args.device)
            X_impact_test_m = torch.full(X_test_m.shape, 100, dtype=torch.long)
            y_impact_test_m = torch.full(y_test_m.shape, 100, dtype=torch.long)

            # Concatenate
            X_test = torch.cat((X_test, X_test_m))
            y_test = torch.cat((y_test, y_test_m))
            X_impact_test = torch.cat((X_impact_test, X_impact_test_m))
            y_impact_test = torch.cat((y_impact_test, y_impact_test_m))

    ########################################################################
    #                            Create DeepSeq                            #
    ########################################################################

    # Initialise deepseq
    deepseq = DeepSeq(
        n_features  = args.features,
        complexity  = args.complexity,
        context     = args.dim_input,
        device      = args.device,
        eps         = args.epsilon,
        min_samples = args.min_samples,
        threshold   = args.threshold,
    )

    ########################################################################
    #                              Load/save                               #
    ########################################################################

    # Load ContextBuilder if necessary
    if args.load_context:
        deepseq.context_builder = ContextBuilder.load(
            infile = args.load_context,
            device = args.device,
        )
    # Fit ContextBuilder
    else:
        deepseq.context_builder.fit(X_train, y_train,
            batch_size = args.batch_size,
            epochs     = args.epochs,
            verbose    = args.verbose,
        )

    # Save ContextBuilder if necessary
    if args.save_context:
        deepseq.context_builder.save(args.save_context)

    # Load Interpreter if necessary
    if args.load_interpreter:
        deepseq.interpreter = Interpreter.load(
            infile          = args.load_interpreter,
            context_builder = deepseq.context_builder,
        )
    # Fit Interpreter
    else:
        # Get scores
        score_y = y_impact_train.unsqueeze(-1)

        # Define score function
        def func_score(X):
            # return X.mean(dim=0)
            return X.max(dim=0).values

        deepseq.interpreter.fit(X_train, y_train,
            score      = score_y.squeeze(1),
            func_score = func_score,
            verbose    = args.verbose,
        )

    # Save Interpreter if necessary
    if args.save_interpreter:
        deepseq.interpreter.save(args.save_interpreter)

    ########################################################################
    #                            Analysis Train                            #
    ########################################################################

    # Predict train data
    result_train = deepseq.interpreter.predict(X_train, y_train, verbose=args.verbose)

    ########################################################################
    #                              Categorize                              #
    ########################################################################

    # True train
    # Initialise impact
    y_impact_train  = y_impact_train[:, 0].cpu().numpy()
    y_true_train    = np.zeros(y_impact_train.shape[0])

    # Set impact
    y_true_train[y_impact_train >=  0] = 1
    y_true_train[y_impact_train >= 30] = 2
    y_true_train[y_impact_train >= 70] = 3

    # Predicted train
    # Initialise results
    y_pred_train    = np.zeros(result_train.shape[0])
    y_pred_train[:] = result_train[:, 0]

    # Set impact - train
    y_pred_train[result_train[:, 0] >=  0] = 0
    y_pred_train[result_train[:, 0] >=  5] = 1
    y_pred_train[result_train[:, 0] >= 30] = 2
    y_pred_train[result_train[:, 0] >= 70] = 3

    ########################################################################
    #                             Manual Mode                              #
    ########################################################################
    print("\n\n")
    box("Manual Mode")

    # Calculate statistics - datapoints
    datapoints  = len(deepseq.interpreter.clusters)
    clustered   = (deepseq.interpreter.clusters != -1).sum()
    anomalies   = (deepseq.interpreter.clusters == -1).sum()
    anomalies_c =  (y_pred_train == -1).sum()
    anomalies_c2 = (y_pred_train  <  0).sum()
    print("Predicted anomalies : {}".format((y_pred_train  <  0).sum()))
    print("Predicted confidence: {}".format((y_pred_train == -1).sum()))
    print("Predicted train     : {}".format((y_pred_train == -2).sum()))
    print("Predicted epsilon   : {}".format((y_pred_train == -3).sum()))
    width       = len(str(datapoints))

    # Calculate statistics - clusters
    clusters = Counter(deepseq.interpreter.clusters)
    # Remove anomaly cluster
    if -1 in clusters: del clusters[-1]
    cluster_counts = np.asarray(list(clusters.values()))
    if cluster_counts.shape[0] == 0:
        cluster_counts = np.asarray([0])

    # Print results
    header("Statistics - Datapoints")
    print("Datapoints            : {:{width}}".format(datapoints, width=width))
    print("Clustered             : {:{width}}/{:{width}} = {:6.2f}%".format(clustered  , datapoints, 100*clustered  /datapoints, width=width))
    print("Anomalies             : {:{width}}/{:{width}} = {:6.2f}%".format(anomalies  , datapoints, 100*anomalies  /datapoints, width=width))
    print("Anomalies < confidence: {:{width}}/{:{width}} = {:6.2f}%".format(anomalies_c, datapoints, 100*anomalies_c/datapoints, width=width))
    print()

    header("Statistics - Clusters")
    print("Labels              : {}".format(len(deepseq.interpreter.tree)))
    print("Clusters            : {}".format(len(clusters)))
    print("Cluster size avarage: {:.4f}".format(cluster_counts.mean()))
    print("Cluster size std    : {:.4f}".format(cluster_counts.std ()))
    print("Cluster size min    : {}"    .format(cluster_counts.min ()))
    print("Cluster size max    : {}"    .format(cluster_counts.max ()))
    print()

    ####################################################################
    #                           Performance                            #
    ####################################################################

    header("Performance")
    mask = y_pred_train >= 0
    print(classification_report(
        y_true        = y_true_train[mask],
        y_pred        = y_pred_train[mask],
        digits        = 4,
        labels        = [0, 1, 2, 3, 4],
        target_names  = ['INFO', 'LOW', 'MEDIUM', 'HIGH', 'ATTACK'],
        zero_division = 0,
    ))

    header("Confusion matrix")
    print(confusion_report(
        y_true        = y_true_train[mask],
        y_pred        = y_pred_train[mask],
        labels        = [0, 1, 2, 3, 4],
        target_names  = ['INFO', 'LOW', 'MEDIUM', 'HIGH', 'ATTACK'],
    ))

    ####################################################################
    #                         Cluster metrics                          #
    ####################################################################

    header("Cluster metrics")
    scores   = deepseq.interpreter.scores
    clusters = deepseq.interpreter.clusters[deepseq.interpreter.clusters != -1]
    scores_c = np.zeros(scores.shape[0])
    scores_c[scores[:, 0] ==  0] = 0
    scores_c[scores[:, 0] >   0] = 1
    scores_c[scores[:, 0] >= 30] = 2
    scores_c[scores[:, 0] >= 70] = 3

    # Keep stats
    first = True
    full_info   = 0
    full_low    = 0
    full_medium = 0
    full_high   = 0
    full_attack = 0
    mixed       = 0

    for cluster, indices in sorted(lookup_table(clusters, verbose=False)):
        # Compute categories
        info    = (scores_c[indices] == 0).sum()
        low     = (scores_c[indices] == 1).sum()
        medium  = (scores_c[indices] == 2).sum()
        high    = (scores_c[indices] == 3).sum()
        attacks = (scores_c[indices] == 4).sum()

        if info == indices.shape[0]:
            full_info += 1
        elif low == indices.shape[0]:
            full_low += 1
        elif medium == indices.shape[0]:
            full_medium += 1
        elif high == indices.shape[0]:
            full_high += 1
        elif attacks == indices.shape[0]:
            full_attack += 1
        # Mixed
        else:
            mixed += 1
            if first:
                print("{:30} {:>8} {:>8} {:>8} {:>8} {:>8}".format("MIXED CLUSTERS", "INFO", "LOW", "MEDIUM", "HIGH", "ATTACK"))
                first = False
            print("Cluster {:5} [size={:8}]: {:8} {:8} {:8} {:8} {:8}".format(
                cluster, indices.shape[0], info, low, medium, high, attacks))

    if not first: print()

    n_clusters = np.unique(clusters).shape[0]
    width = len(str(n_clusters))
    print("Clusters           : {}".format(n_clusters))
    print("Clusters INFO      : {:>{width}}".format(full_info  , width=width))
    print("Clusters LOW       : {:>{width}}".format(full_low   , width=width))
    print("Clusters MEDIUM    : {:>{width}}".format(full_medium, width=width))
    print("Clusters HIGH      : {:>{width}}".format(full_high  , width=width))
    print("Clusters ATTACK    : {:>{width}}".format(full_attack, width=width))
    print("Clusters MIXED     : {:>{width}}".format(mixed      , width=width))
    print()

    print("Homogeneity overall: {:.4f}".format(homogeneity_score(scores_c, clusters)))
    print("\n\n")

    ########################################################################
    #                            Analysis Test                             #
    ########################################################################

    # Predict test data
    result_test = deepseq.interpreter.predict(
        X_test,
        y_test,
        verbose = args.verbose,
    )

    ################################################################
    #                          Categorize                          #
    ################################################################

    # True train
    # Initialise impact
    y_impact_test  = y_impact_test[:, 0].cpu().numpy()
    y_true_test    = np.zeros(y_impact_test.shape[0], dtype=int)

    # Set impact
    y_true_test[y_impact_test >=  0] = 1
    y_true_test[y_impact_test >= 30] = 2
    y_true_test[y_impact_test >= 70] = 3

    # Predicted train
    # Initialise results
    y_pred_test    = np.zeros(result_test.shape[0], dtype=int)
    y_pred_test[:] = result_test[:, 0]

    # Set impact - train
    y_pred_test[result_test[:, 0] >=  0] = 0
    y_pred_test[result_test[:, 0] >=  5] = 1
    y_pred_test[result_test[:, 0] >= 30] = 2
    y_pred_test[result_test[:, 0] >= 70] = 3

    ################################################################
    #                        Automatic mode                        #
    ################################################################

    box("Automatic Mode")
    print()

    header("Statistics - Workload Reduction")
    datapoints = y_pred_test.shape[0]
    automated  = (y_pred_test >= 0).sum()
    anomalous  = (y_pred_test <  0).sum()
    anomalies_conf  = (y_pred_test == -1).sum()
    anomalies_train = (y_pred_test == -2).sum()
    anomalies_eps   = (y_pred_test == -3).sum()
    width = len(str(datapoints))
    print("Datapoints            : {:{width}}".format(datapoints, width=width))
    print("Automated             : {:{width}}/{:{width}} = {:6.2f}%".format(automated, datapoints, 100*automated/datapoints, width=width))
    print("Anomalous             : {:{width}}/{:{width}} = {:6.2f}%".format(anomalous, datapoints, 100*anomalous/datapoints, width=width))
    print("Anomalous confidence  : {:{width}}/{:{width}} = {:6.2f}%".format(anomalies_conf, datapoints, 100*anomalies_conf/datapoints, width=width))
    print("Anomalous not in train: {:{width}}/{:{width}} = {:6.2f}%".format(anomalies_train, datapoints, 100*anomalies_train/datapoints, width=width))
    print("Anomalous > epsilon   : {:{width}}/{:{width}} = {:6.2f}%".format(anomalies_eps, datapoints, 100*anomalies_eps/datapoints, width=width))
    print()

    header("Statistics - Anomalies")
    y_true_anomalous = y_true_test[y_pred_test < 0]
    y_pred_anomalous = y_pred_test[y_pred_test < 0]

    print(confusion_report(
        y_true = y_true_anomalous,
        y_pred = y_pred_anomalous,
        labels = [-3, -2, -1, 0, 1, 2, 3, 4],
        target_names  = ['CONF', 'TRAIN', 'EPS',
                         'INFO', 'LOW', 'MEDIUM', 'HIGH', 'ATTACK'],
        skip_x = ['INFO', 'LOW', 'MEDIUM', 'HIGH', 'ATTACK'],
        skip_y = ['CONF', 'TRAIN', 'EPS'],
    ))

    header("Performance")
    mask = y_pred_test >= 0
    print(classification_report(
        y_true        = y_true_test[mask],
        y_pred        = y_pred_test[mask],
        digits        = 4,
        labels        = [0, 1, 2, 3, 4],
        target_names  = ['INFO', 'LOW', 'MEDIUM', 'HIGH', 'ATTACK'],
        zero_division = 0,
    ))

    header("Confusion matrix")
    print(confusion_report(
        y_true        = y_true_test[mask],
        y_pred        = y_pred_test[mask],
        labels        = [0, 1, 2, 3, 4],
        target_names  = ['INFO', 'LOW', 'MEDIUM', 'HIGH', 'ATTACK'],
    ))