import os
import pickle
import numpy as np
import pandas as pd
from scipy.sparse import hstack
import datetime
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import TimeSeriesSplit, cross_val_score, GridSearchCV
from sklearn.metrics import roc_auc_score
from sklearn.linear_model import LogisticRegression
from datetime import date
from contextlib import contextmanager
import time

PATH_TO_DATA = 'data/'
path_to_train = os.path.join(PATH_TO_DATA, 'train_sessions.csv')
path_to_test = os.path.join(PATH_TO_DATA, 'test_sessions.csv')
path_to_site_dict = os.path.join(PATH_TO_DATA, 'site_dic.pkl')
AUTHOR = 'Aman_Berry'

r = 42
N_JOBS = -1
NUM_TIME_SPLITS = 10    # for time-based cross-validation
SITE_NGRAMS = (1, 10)    # site ngrams for "bag of sites"
MAX_FEATURES = 60000    # max features for "bag of sites"
BEST_LOGIT_C = 1.623776739188721  # precomputed tuned C for logistic regression


# nice way to report running times
@contextmanager
def timer(name):
    t0 = time.time()
    yield
    print(f'[{name}] done in {time.time() - t0:.0f} s')

with open(path_to_site_dict, 'rb') as f:
     site_dict = pickle.load(f)

def prepare_sparse_features(path_to_train, path_to_test, path_to_site_dict,
                           vectorizer_params):
    times = ['time%s' % i for i in range(1, 11)]
    train_df = pd.read_csv(path_to_train,
                       index_col='session_id', parse_dates=times)
    test_df = pd.read_csv(path_to_test,
                      index_col='session_id', parse_dates=times)

    # Sort the data by time
    train_df = train_df.sort_values(by='time1')

    # read site -> id mapping provided by competition organizers
    with open(path_to_site_dict, 'rb') as f:
        site2id = pickle.load(f)
    # create an inverse id _> site mapping
    id2site = {v:k for (k, v) in site2id.items()}
    # we treat site with id 0 as "unknown"
    id2site[0] = 'unknown'

    # Transform data into format which can be fed into TfidfVectorizer
    # This time we prefer to represent sessions with site names, not site ids.
    # It's less efficient but thus it'll be more convenient to interpret model weights.
    sites = ['site%s' % i for i in range(1, 11)]
    train_sessions = train_df[sites].fillna(0).astype('int').apply(lambda row:
                                                     ' '.join([id2site[i] for i in row]), axis=1).tolist()
    test_sessions = test_df[sites].fillna(0).astype('int').apply(lambda row:
                                                     ' '.join([id2site[i] for i in row]), axis=1).tolist()
    # we'll tell TfidfVectorizer that we'd like to split data by whitespaces only
    # so that it doesn't split by dots (we wouldn't like to have 'mail.google.com'
    # to be split into 'mail', 'google' and 'com')
    vectorizer = TfidfVectorizer(**vectorizer_params)
    X_train = vectorizer.fit_transform(train_sessions)
    X_test = vectorizer.transform(test_sessions)
    y_train = train_df['target'].astype('int').values

    # we'll need site visit times for further feature engineering
    train_times, test_times = train_df[times], test_df[times]
    trainer = pd.read_csv(path_to_train,
                           index_col='session_id').sort_values(by='time1')
    tester = pd.read_csv(path_to_test,
                          index_col='session_id')
    train_sites = trainer[sites]
    test_sites = tester[sites]

    return X_train, X_test, y_train, vectorizer, train_times, test_times, train_sites, test_sites

youtube_ids = []

for key in list(site_dict.keys()):
    if 'youtube' in key:
        youtube_ids.append(site_dict[key])

def add_features(times, X_sparse, sites):
    scaler = StandardScaler()
    hour = times['time1'].apply(lambda ts: ts.hour)
    morning = ((hour >= 7) & (hour <= 11)).astype('int').values.reshape(-1, 1)
    day = ((hour >= 12) & (hour <= 18)).astype('int').values.reshape(-1, 1)
    evening = ((hour >= 19) & (hour <= 23)).astype('int').values.reshape(-1, 1)
    night = ((hour >= 0) & (hour <= 6)).astype('int').values.reshape(-1, 1)
    sess_duration = scaler.fit_transform((times.max(axis=1) - times.min(axis=1)).astype('timedelta64[s]')\
		   .astype('int').values.reshape(-1, 1))
    day_of_week = times['time1'].apply(lambda t: t.weekday()).values.reshape(-1, 1)
    month = scaler.fit_transform(times['time1'].apply(lambda t: t.month).values.reshape(-1, 1))
    year_month = scaler.fit_transform(times['time1'].apply(lambda t: 100 * t.year + t.month).values.reshape(-1, 1))
    weekend_ints = [5,6] # 5 saturday, 6 sunday
    dow = times['time1'].dt.dayofweek
    weekend = dow.apply(lambda x: 1 if x in weekend_ints else 0).astype('int64').values.reshape(-1,1)
    alice_hours = [9,11,12,13,14,15,16,17,18] # the hours which Alice is primarily online
    alice_hour = hour.apply(lambda x: 1 if x in alice_hours else 0).values.reshape(-1,1)
    youtube = sites['site1'].apply(lambda x: 1 if x in youtube_ids else 0).values.reshape(-1,1) # checking if first site was Youtube
    uniques = sites.count(axis=1).values.reshape(-1,1)

    X = hstack([X_sparse, morning, day, evening, night, sess_duration, day_of_week, month, year_month, weekend, alice_hour, youtube, uniques])
    return X


with timer('Building sparse site features'):
    X_train_sites, X_test_sites, y_train, vectorizer, train_times, test_times, train_sites, test_sites = \
        prepare_sparse_features(
            path_to_train=os.path.join(PATH_TO_DATA, 'train_sessions.csv'),
            path_to_test=os.path.join(PATH_TO_DATA, 'test_sessions.csv'),
            path_to_site_dict=os.path.join(PATH_TO_DATA, 'site_dic.pkl'),
            vectorizer_params={'ngram_range': SITE_NGRAMS,
                               'max_features': MAX_FEATURES,
                               'tokenizer': lambda s: s.split()})


with timer('Building additional features'):
    X_train_final = add_features(train_times, X_train_sites, train_sites)
    X_test_final = add_features(test_times, X_test_sites, test_sites)


with timer('Cross-validation'):
    time_split = TimeSeriesSplit(n_splits=NUM_TIME_SPLITS)
    logit = LogisticRegression(random_state=r, solver='liblinear')

    # I've done cross-validation locally, and do not reproduce these heavy computations here
    c_values = [BEST_LOGIT_C]

    logit_grid_searcher = GridSearchCV(estimator=logit, param_grid={'C': c_values},
                                  scoring='roc_auc', n_jobs=N_JOBS, cv=time_split, verbose=1)
    logit_grid_searcher.fit(X_train_final, y_train)
    print('CV score', logit_grid_searcher.best_score_)


with timer('Test prediction and submission'):
    test_pred = logit_grid_searcher.predict_proba(X_test_final)[:, 1]
    pred_df = pd.DataFrame(test_pred, index=np.arange(1, test_pred.shape[0] + 1),
                       columns=['target'])
    pred_df.to_csv(f'submission_alice_{AUTHOR}.csv', index_label='session_id')
