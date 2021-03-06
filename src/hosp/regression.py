"""
The module applies linear regression for final-value prediction. Compatible with Python 3.4+.

Functionalities include:
    1. Cross-validation
    2. Report Generation
    3. Prediction
"""
# first party
import flu_contest.src.hosp.preparation as preparation
import flu_contest.src.hosp.hosp_utils as hosp_utils
import flu_contest.src.hosp.ml_utils as ml_utils
import utils.epiweek as utils

from flu_contest.src.hosp.tools import *
from flu_contest.src.hosp.constants import *
# third party
import os
import pickle
import numpy as np
import matplotlib.pyplot as plt

from tqdm import tqdm
from sklearn import metrics

def train_model(X, Y, model_type):
    """
    build SVR regression model for final-value prediction.

    Args:
        X - training input
        Y - training output
        model_type - the name of machine learning model
    
    Returns:
        model - the trained machine learning model
    """
    # determine if co-training is necessary
    cotrain = (model_type == 'quant_lin' or model_type == 'quant_ridge')

    if cotrain:
        if model_type == 'quant_lin':
            model_upper = ml_utils.QuantLinear(UPPER_QUANT)
            model = ml_utils.QuantLinear(MID_QUANT)
            model_lower = ml_utils.QuantLinear(LOWER_QUANT)
            ml_utils.QuantLinear.fit(model_upper, model, model_lower, X, Y)
        elif model_type == 'quant_ridge':
            model_upper = ml_utils.QuantRidge(UPPER_QUANT)
            model = ml_utils.QuantRidge(MID_QUANT)
            model_lower = ml_utils.QuantRidge(LOWER_QUANT)
            ml_utils.QuantRidge.fit(model_upper, model, model_lower, X, Y)
    else:
        if model_type == 'lin':
            model_upper = ml_utils.RegLinear(UPPER_QUANT)
            model = ml_utils.RegLinear(MID_QUANT)
            model_lower = ml_utils.RegLinear(LOWER_QUANT)
        elif model_type == 'ridge':
            model_upper = ml_utils.RegRidge(UPPER_QUANT)
            model = ml_utils.RegRidge(MID_QUANT)
            model_lower = ml_utils.RegRidge(LOWER_QUANT)
        elif model_type == 'gbdt':
            model_upper = ml_utils.RegGBDT(UPPER_QUANT)
            model = ml_utils.RegGBDT(MID_QUANT)
            model_lower = ml_utils.RegGBDT(LOWER_QUANT)
        elif model_type == 'quant_gbdt':
            model_upper = ml_utils.QuantGBDT(UPPER_QUANT)
            model = ml_utils.QuantGBDT(MID_QUANT)
            model_lower = ml_utils.QuantGBDT(LOWER_QUANT)
        
        model_upper.fit(X, Y)
        model.fit(X, Y)
        model_lower.fit(X, Y)

    return model_upper, model, model_lower

def validate(data, 
            locations, groups, 
            time_period, lag, 
            left_window, right_window, backfill_window,
            mode, model_type):
    """
    apply cross-validation for a time period with other seasons
    as training data.

    Args:
        data - the data source
        locations - the list of locations for machine learning model
        groups - the list of groups for machine learning model
        time_period - a string representing the starting and ending epiweek
        lag - the current time lag
        left_window, right_window - the time period considered for regression: 
            [cur_time - left_window, cur_time + right_window]
        backfill_window - the backfill period: [cur_time-window, cur_time]
        mode - the training scheme.
            prev: use the seasons within year window as training data
            all: use all previous seasons as training data
        model_type - the name of machine learning model
    
    Returns:
        valid_weeks - the active epiweeks for flu
        predictions, prediction_upper, prediction_lower - 
            the predicted final values, the latter two form confidence intervals
        cur_truth - the final value of series at current time
        ground_truth - the final values provided by CDC after backfill
    """
    start_year = hosp_utils.get_start_year(time_period[0])
    period = hosp_utils.unravel(time_period)
    model_periods = []

    # obtain all training seasons
    if mode == 'all':
        for year in range(FIRST_YEAR, start_year):
            model_periods.append(hosp_utils.get_period(year, 40, 17))
    elif mode == 'prev':
        for year in range(max(FIRST_YEAR, start_year - YEAR_WINDOW), start_year):
            model_periods.append(hosp_utils.get_period(year, 40, 17))
    # train models
    X, Y = preparation.prepare(data, locations, groups, 
                                model_periods, lag, 
                                left_window, right_window, backfill_window)
    model_upper, model, model_lower = train_model(X, Y, model_type)
    # result collectors
    predictions = create_double_list([], len(locations), len(groups))
    predictions_lower = create_double_list([], len(locations), len(groups))
    predictions_upper = create_double_list([], len(locations), len(groups))
    ground_truth = create_double_list([], len(locations), len(groups))
    cur_truth = create_double_list([], len(locations), len(groups))
    valid_weeks = []

    for epiweek in period:
        valid_weeks.append(epiweek)

        for l_idx in range(len(locations)):
            location = locations[l_idx]

            for g_idx in range(len(groups)):
                group = groups[g_idx]

                # if data exists, perform cross-validation
                if (epiweek, group, location) in data:
                    x_val, cur_y_val, y_val = preparation.fetch(data, location, group, 
                                                                epiweek, lag, 
                                                                left_window, right_window, 
                                                                backfill_window)
                    pred = model.predict(x_val.T)
                    pred_u = model_upper.predict(x_val.T)
                    pred_l = model_lower.predict(x_val.T)
                    # record results
                    predictions[l_idx][g_idx].append(pred)
                    predictions_lower[l_idx][g_idx].append(pred_l)
                    predictions_upper[l_idx][g_idx].append(pred_u)
                    # record ground truth
                    cur_truth[l_idx][g_idx].append(cur_y_val)
                    ground_truth[l_idx][g_idx].append(y_val)
    
    return valid_weeks, predictions, predictions_lower, predictions_upper, \
        cur_truth, ground_truth

def nowcast_report(path, data, 
                    locations, groups, 
                    time_period, 
                    left_window, backfill_window, 
                    mode, model_type):
    """
    calculate metric for linear regression and plot prediction results.

    Args:
        path - the path where tables and graphs are generated.
        data - the data source
        locations - the locations to report
        groups - the groups to report
        time_period - a string representing the starting and ending epiweek
        left_window - the time period considered for linear regression: 
            [cur_time-left_window, cur_time]
        backfill_window - the time period considered for backfill: 
            [cur_time-backfill_window, cur_time]
        mode - the training scheme.
            prev: use the seasons within year window as training data
            all: use all previous seasons as training data
        model_type - the machine learning model used
    
    Returns:
        rsq - the explained variance statistics of prediction
        mse - the mean squared error of prediction
    """
    # result collectors
    rsq = create_double_list(None, len(locations), len(groups))
    mse = create_double_list(None, len(locations), len(groups))
    # run cross validation and calculate statistics
    valid_weeks, \
    predictions, predictions_lower, predictions_upper, \
    cur_truth, ground_truth = \
    validate(data, 
            locations, groups,
            time_period, lag=0, 
            left_window=left_window, right_window=0, 
            backfill_window=backfill_window, 
            mode=mode, model_type=model_type)

    for l_idx in range(len(locations)):
        location = locations[l_idx]

        for g_idx in range(len(groups)): 
            group = groups[g_idx]
            rsq[l_idx][g_idx] = metrics.r2_score(
                                ground_truth[l_idx][g_idx], predictions[l_idx][g_idx])
            mse[l_idx][g_idx] = metrics.mean_squared_error(
                                ground_truth[l_idx][g_idx], predictions[l_idx][g_idx])
            # plot with epiweeks as ticks for x-axis
            inds = range(len(valid_weeks))
            week_ticks = [str(epiweek % 100) for epiweek in valid_weeks]
            # plot predicted rate, true rate, and current rate
            plt.figure()
            plt.plot(inds, predictions[l_idx][g_idx], label='predicted rate')
            plt.plot(inds, predictions_upper[l_idx][g_idx], label='predicted rate upper bound')
            plt.plot(inds, predictions_lower[l_idx][g_idx], label='predicted rate lower bound')
            plt.plot(inds, cur_truth[l_idx][g_idx], label='current rate')
            plt.plot(inds, ground_truth[l_idx][g_idx], label='true rate')
            plt.xticks(inds, week_ticks, rotation='vertical')
            plt.xlabel('weeks')
            plt.ylabel('hospitalized rate')
            plt.legend()
            plt.savefig(path + '/' + location + '/' + str(group) + '-' +
                        str(left_window) + '-' + str(backfill_window) + '.png', dpi=300)
            plt.close()
        
    return rsq, mse

def plot_results(path, results, name, location, group, colormap):
    """
    plot the mse / rsq results as heatmap for each location and group.

    Args:
        path - the path where heatmaps are generated.
        data - the mse / rsq results
        name - the description of result (mse or rsq)
        locations - the location to report
        groups - the group to report
        colormap - the colormap used for heatmap
    
    Returns:
        None
    """
    length, width = results.shape
    fig, ax = plt.subplots()
    ax.imshow(results, cmap=colormap)

    ax.set_title(GROUP_DESCRIPTIONS[group])
    ax.set_ylabel('window')
    ax.set_xlabel('backfill')
    ax.set_yticks(range(length))
    ax.set_xticks(range(width))

    # fill each square by statistic
    for i in range(length):
        for j in range(i+1):
            ax.text(j, i, '{:.2f}'.format(results[i][j]), ha='center', va='center', color='w')
    
    fig.tight_layout()
    plt.savefig(path + '/' + location + '/' + name + '_results_' + str(group) + '.png', dpi=300)
    plt.close(fig)

def run_nowcast_experiment(data, 
                            location_groups, groupings, 
                            time_periods, 
                            max_window, 
                            mode, model_type):
    """
    run nowcast experiments for different time periods.

    Args:
        data - the data source (included all epiweeks with all lags)
        location_groups - the groupings for states
        groupings - the groupings for age groups
        time_periods - a list of time period (strings representing the starting and ending epiweek)
        max_window - the maximum time window considered in experiment
        mode - the training scheme.
            prev: use the seasons within year window as training data
            all: use all previous seasons as training data
        model_type - the machine learning model used
    
    Returns:
        None
    """
    # matrix for recording results, will be copied for each entry
    results = {}
    mse_record_mat = np.full((max_window + 1, max_window + 1), -np.inf)
    rsq_record_mat = np.full((max_window + 1, max_window + 1), -np.inf)

    for period in time_periods:
        # create path for writing reports
        report_path = './nowcast/'+str(period)
        if not os.path.exists(report_path):
            os.makedirs(report_path)
        # create path for each state
        for state in STATE_LIST:
            cur_path = report_path + '/' + state
            if not os.path.exists(cur_path):
                os.makedirs(cur_path)
        # obtain results for each group and location under grouping
        for locations in tqdm(location_groups):
            for groups in groupings:
                mse_results = create_double_list(mse_record_mat, len(locations), len(groups))
                rsq_results = create_double_list(rsq_record_mat, len(locations), len(groups))
                # record the result for each time window and backfill
                for window in tqdm(range(max_window + 1)):
                    for backfill in range(window + 1):
                        rsq, mse = nowcast_report(report_path, data, 
                                                locations, groups, 
                                                period, 
                                                left_window=window, backfill_window=backfill, 
                                                mode=mode, model_type=model_type)

                        for l_idx in range(len(locations)):
                            for g_idx in range(len(groups)):
                                mse_results[l_idx][g_idx][window][backfill] = mse[l_idx][g_idx]
                                rsq_results[l_idx][g_idx][window][backfill] = rsq[l_idx][g_idx]
                # plot the results
                for l_idx in range(len(locations)):
                    location = locations[l_idx]

                    for g_idx in range(len(groups)):
                        group = groups[g_idx]

                        plot_results(report_path, mse_results[l_idx][g_idx], 
                                    'mse', location, group, 'Reds')
                        plot_results(report_path, rsq_results[l_idx][g_idx], 
                                    'rsq', location, group, 'Blues')
                        # set the maximum as model metric
                        results[(period, group)] = np.amax(rsq_results[l_idx][g_idx])

    write_data(results, './nowcast/results.txt')

def predict(data, epiweek, 
            location_groups, groupings, 
            max_window, 
            mode, model_type):
    """
    #TODO: support location / age group combination

    perform prediction for specific epiweek.
    
    choose slabs (i.e. window and backfill window) as following:
        consider combinations (0, 0), (1, 1), (2, 2),
        add R^2 penalty of 0.04 * window_size

    Args:
        data - the data source
        epiweek - the epiweek for which we predict the final value
        location_groups - the grouping of all locations
        groupings - the grouping of all age groups
        max_window - the maximum time window considered in modeling
        mode - the training scheme.
            prev: use the seasons within year window as training data
            all: use all previous seasons as training data
        model_type - the type of machine learning model used
    
    Returns:
        preds, preds_upper, preds_lower - the prediction results
    """
    # create cross-validation period
    start_year = hosp_utils.get_start_year(epiweek)
    max_window = min(max_window, hosp_utils.get_max_window(epiweek))
    val_period = hosp_utils.get_period(start_year - 1, 40, 17)
    preds, preds_upper, preds_lower = {}, {}, {}

    for lg_idx in range(len(location_groups)):
        locations = location_groups[lg_idx]

        for gp_idx in range(len(groupings)):
            groups = groupings[gp_idx]

            cur_preds = create_double_list([], len(locations), len(groups))
            cur_preds_upper = create_double_list([], len(locations), len(groups))
            cur_preds_lower = create_double_list([], len(locations), len(groups))

            opt_window = 0
            cur_rsq = 0.0

            # perform cross-validation and select the optimal hyperparameters, 
            # i.e. window and backfill
            for window in range(0, max_window + 1):
                _, predictions, _, _, _, ground_truth = validate(data, 
                                                        locations, groups, 
                                                        val_period, lag=0, 
                                                        left_window=window, right_window=0, 
                                                        backfill_window=window,
                                                        model_type=model_type, mode=mode)
                # calculate metrics and compare
                predictions = np.vstack(flatten_double_list(predictions)).squeeze()
                ground_truth = np.vstack(flatten_double_list(ground_truth)).squeeze()
                rsq = metrics.r2_score(ground_truth, predictions) - PENALTY * window

                if rsq > cur_rsq:
                    opt_window = window
                    cur_rsq = rsq
            
            # use validation period (i.e. the latest) to train the model
            # and then make predictions
            X, Y = preparation.prepare(data, 
                                    locations, groups, 
                                    [val_period], lag=0, 
                                    left_window=opt_window, right_window=0, 
                                    backfill_window=opt_window)
            # get trained model and residuals
            model_upper, model, model_lower = train_model(X, Y, model_type)

            for l_idx in range(len(locations)):
                location = locations[l_idx]

                for g_idx in range(len(groups)):
                    group = groups[g_idx]
                    X_pred, _, _ = preparation.fetch(data, 
                                                    location, group, 
                                                    epiweek, lag=0, 
                                                    left_window=opt_window, right_window=0, 
                                                    backfill_window=opt_window)
                    # get results from prediction 
                    pred_u = model_upper.predict(X_pred.T)
                    pred = model.predict(X_pred.T)
                    pred_l = model_lower.predict(X_pred.T)
                    # add to records
                    cur_preds[l_idx][g_idx].append(pred)
                    cur_preds_upper[l_idx][g_idx].append(pred_u)
                    cur_preds_lower[l_idx][g_idx].append(pred_l)
            
            for l_idx in range(len(locations)):
                location = locations[l_idx]

                for g_idx in range(len(groups)):
                    group = groups[g_idx]
                    # assign predictions to locations and groups
                    preds[(location, group)] = cur_preds[l_idx][g_idx][0][0]
                    preds_upper[(location, group)] = cur_preds_upper[l_idx][g_idx][0][0]
                    preds_lower[(location, group)] = cur_preds_lower[l_idx][g_idx][0][0]

    return preds, preds_upper, preds_lower
