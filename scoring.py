# scoring.py
import pandas as pd
import json
from typing import List, Dict
from sklearn.linear_model import LinearRegression
import numpy as np
import requests
import subprocess
import multiprocessing
import os
from sklearn.preprocessing import MinMaxScaler


def compute_city_scores(group_preferences, maximum_group_budgets, eps=1e-6):

    filepath = "cities.csv"
    df_valid = pd.read_csv(filepath)

    # Example group input
    group_origins = ["LIS", "BLA", "MUC"]  # Each user's origin airport code
    departure_date = "2025-08-01"
    return_date = "2025-08-15"

    relevant_criteria = ["adult_nightlife", "happiness_level", "fun", "walkability", "friendly_to_foreigners"]
    
    df_budget_filtered = filter_by_flight_budget(df_valid, maximum_group_budgets, group_origins, departure_date)

    if df_budget_filtered.empty:
        print("\n⚠️ No valid destinations remain within the given budget constraints.")
        print("   ➤ Consider raising max budgets or reviewing available flights.")
        exit()
    regression_recommendations = recommend_via_similarity(df_budget_filtered, relevant_criteria, group_preferences)

    print("Top recommended cities via linear regression (with flight-budget filter):")
    print(regression_recommendations)

   
    return regression_recommendations


# Fetch real-time flight prices from Skyscanner API (mock structure)
def fetch_flight_prices(origin: str, destinations: List[str], departure_date: str) -> Dict[str, float]:

    prices = {}
    
    # Load the precomputed CSV with flight prices
    df_prices = pd.read_csv("flight_prices.csv")

    # Filter only rows that match the origin and departure date
    df_filtered = df_prices[(df_prices['origin'] == origin) & 
                            (df_prices['departure_date'] == departure_date)]

    # Create the price dictionary for destinations
    for dest in destinations:
        match = df_filtered[df_filtered['destination'] == dest]
        if not match.empty:
            # Assume the lowest price if there are multiple
            prices[dest] = match['price'].min()

    return prices

    
# Filter destinations by flight budget constraints for each user
def filter_by_flight_budget(df: pd.DataFrame, maximum_group_budgets: List[int], group_origins: List[str], departure_date: str) -> pd.DataFrame:
    remaining_df = df.copy()
    destination_iatas = df['IATA'].tolist()
    
    for origin, max_budget in zip(group_origins, maximum_group_budgets):
        prices = fetch_flight_prices(origin, destination_iatas, departure_date)

        # Filter to destinations affordable for this user
        affordable_iatas = [
            iata for iata in destination_iatas
            if iata in prices and 2 * prices[iata] <= max_budget
        ]
    
        remaining_df = remaining_df[remaining_df['IATA'].isin(affordable_iatas)]
        destination_iatas = affordable_iatas  # Limit to next user's options

        # Early stop: if no destination survives for this user
        if not destination_iatas:
            return pd.DataFrame(columns=df.columns)

    # Attach flight prices from the last iteration
    # remaining_df['flight_price'] = remaining_df['IATA'].map(prices)
    # return remaining_df.reset_index(drop=True)
    return remaining_df


# Recommend cities using linear regression to minimize loss to group preference vector
def recommend_via_similarity(df: pd.DataFrame, keys: list[str], group_preferences: list[list[str]], top_k: int = 5) -> pd.DataFrame:
    df = df.copy()

    # Step 1: Normalize feature values to [1, 5]
    scaler = MinMaxScaler(feature_range=(1, 5))
    df[keys] = scaler.fit_transform(df[keys])

    # Step 2: Convert group preferences to numeric array
    group_pref_array = np.array(group_preferences, dtype=float)  # shape: (num_users, num_criteria)

    # Step 3: Compute normalized group preference vector (mean over users)
    group_vector = group_pref_array.mean(axis=0)  # shape: (num_criteria,)

    # Step 4: Compute squared Euclidean distance from each city to group vector
    city_vectors = df[keys].values
    loss = np.linalg.norm(city_vectors - group_vector, axis=1)

    # Step 5: Store and sort by loss
    df["similarity_loss"] = loss
    return df.sort_values(by="similarity_loss").head(top_k)[['city', 'similarity_loss'] + keys]


