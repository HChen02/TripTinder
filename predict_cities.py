import pandas as pd
import json
from typing import List, Dict
from sklearn.linear_model import LinearRegression
import numpy as np
import requests
import subprocess
import multiprocessing
import os


# Load and filter dataset
def load_filtered_data(filepath: str) -> pd.DataFrame:
    relevant_keys = [
        "nightlife_and_entertainment",
        "underrated_destinations",
        "beach",
        "art_and_culture",
        "great_food",
        "outdoor_adventures"
    ]

    df = pd.read_csv(filepath)
    vibes_dicts = df['vibes'].dropna().apply(json.loads)
    vibe_features_df = pd.DataFrame(vibes_dicts.tolist())[relevant_keys].astype(float)
    vibe_features_df.index = df[df['vibes'].notnull()].index

    df_full = df.join(vibe_features_df)
    df_valid = df_full[df_full[relevant_keys].notnull().all(axis=1)].reset_index(drop=True)
    return df_valid, relevant_keys

# Fetch real-time flight prices from Skyscanner API (mock structure)

def fetch_flight_prices(origin: str, destinations: List[str], date_out: str, date_in: str, api_key: str, core_index: int) -> Dict[str, float]:
    prices = {}
    output_file = f"results_{core_index}.txt"    
    for dest in destinations:
        # Call SkyScanner API
        print("Calling API:", core_index, origin, dest)
        result = subprocess.run(["bash", "skyscanner_search.sh", origin, dest, str(core_index)], capture_output=True, text=True)
        try:
            with open(output_file, "r") as f:
                data = json.load(f)
            itineraries = data.get("content", {}).get("results", {}).get("itineraries", {})
            cheapest_price = float('inf')
            for itinerary_id, itinerary in itineraries.items():
                options = itinerary.get("pricingOptions", [])
                for option in options:
                    amount_str = option.get("price", {}).get("amount", "")
                    if not amount_str or not amount_str.isdigit():
                        continue  # skip invalid prices
                    price = int(amount_str) / 1000
                    if price < cheapest_price:
                        cheapest_price = price
            prices[dest] = cheapest_price
            print("Cheapest price:", cheapest_price)
        except FileNotFoundError:
            print("results_", core_index, ".txt not found. Make sure the script ran correctly.")

    return prices

# Filter destinations by flight budget constraints for each user
def filter_by_flight_budget(df: pd.DataFrame, user_budgets: List[Dict[str, int]], group_origins: List[str], departure_date: str, return_date: str, api_key: str, core_index: int) -> pd.DataFrame:
    remaining_df = df.copy()
    destination_iatas = df['IATA'].tolist()
    final_prices = {}

    for origin, budget in zip(group_origins, user_budgets):
        prices = fetch_flight_prices(origin, destination_iatas, departure_date, return_date, api_key, core_index)

        # Keep only affordable destinations for this user
        affordable_iatas = [iata for iata, price in prices.items() if budget['min'] <= price <= budget['max']]
        remaining_df = remaining_df[remaining_df['IATA'].isin(affordable_iatas)]
        destination_iatas = affordable_iatas  # Reduce next loop to filtered destinations

        # Track max flight price per destination
        for iata in affordable_iatas:
            final_prices[iata] = max(final_prices.get(iata, 0), prices[iata])

    # Map back flight prices to remaining destinations
    remaining_df['flight_price'] = remaining_df['IATA'].map(final_prices)
    return remaining_df.reset_index(drop=True)


# Recommend cities using linear regression to minimize loss to group preference vector
def recommend_via_similarity(df: pd.DataFrame, keys: List[str], group_preferences: List[List[str]], top_k: int = 5) -> pd.DataFrame:
    weight_map = {0: 3, 1: 2, 2: 1}
    group_vector = np.zeros(len(keys))
    key_index = {key: i for i, key in enumerate(keys)}

    # Build the weighted group preference vector
    for user in group_preferences:
        for idx, pref in enumerate(user):
            if pref in key_index:
                group_vector[key_index[pref]] += weight_map.get(idx, 0)

    group_vector /= len(group_preferences)

    # Compute Euclidean distance to the group vector
    X = df[keys].values
    loss = np.linalg.norm(X - group_vector, axis=1)

    df = df.copy()
    df['regression_loss'] = loss

    return df.sort_values(by='regression_loss').head(top_k)[['en-GB', 'regression_loss', 'flight_price'] + keys]



def parallel_filter(df_valid, group_budgets, group_origins, departure_date, return_date, api_key, num_cores=None):
    num_cores = multiprocessing.cpu_count()
    df_chunks = np.array_split(df_valid, num_cores)

    arguments = [
        (chunk, group_budgets, group_origins, departure_date, return_date, api_key, i)
        for i, chunk in enumerate(df_chunks)
    ]

    with multiprocessing.Pool(num_cores) as pool:
        results = pool.starmap(filter_by_flight_budget, arguments)

    return pd.concat(results)



# Example usage
if __name__ == "__main__":
    filepath = "small_dataset.csv"
    df_valid, feature_keys = load_filtered_data(filepath)

    # Example group input
    group_origins = ["LON", "BER", "CDG"]  # Each user's origin airport code
    departure_date = "2025-08-01"
    return_date = "2025-08-15"
    skyscanner_api_key = "sh967490139224896692439644109194"  # Replace with your actual API key

    group_preferences = [
        ["beach", "great_food", "nightlife_and_entertainment"],
        ["outdoor_adventures", "art_and_culture", "great_food"],
        ["art_and_culture", "beach", "underrated_destinations"]
    ]

    group_budgets = [
        {"min": 100, "max": 400},
        {"min": 150, "max": 450},
        {"min": 120, "max": 500}
    ]
    
    # Filter and recommend
    #df_budget_filtered = filter_by_flight_budget(df_valid, group_budgets, group_origins, departure_date, return_date, skyscanner_api_key)
    df_budget_filtered = parallel_filter(df_valid, group_budgets, group_origins, departure_date, return_date, skyscanner_api_key)
    
    ##Solve return flight!!!


    regression_recommendations = recommend_via_similarity(df_budget_filtered, feature_keys, group_preferences)

    print("Top recommended cities via linear regression (with flight-budget filter):")
    print(regression_recommendations)
