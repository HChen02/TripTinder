from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
import selenium.webdriver.support.ui as ui
import selenium.webdriver.support.expected_conditions as EC
from selenium.webdriver.chrome.service import Service

import requests
import pandas as pd
from bs4 import BeautifulSoup
from typing import Dict, List, Optional
import json
import re
import os
import time
from time import sleep

def extract_city_data(city_element) -> Dict:
    """
    Extract data from a city element in the HTML.
    
    Args:
        city_element: BeautifulSoup object that contains the HTML content of a city
        
    Returns:
        Dictionary with the information of the city
    """
    data = {}
    
    # Basic Information
    data['city'] = city_element.select_one('h2.itemName a').text.strip()
    data['country'] = city_element.select_one('h3.itemSub a').text.strip()
    data['rank'] = int(city_element.select_one('div.rank').text.strip())
    
    # ratings
    overall_score_width = city_element.select_one('.rating-main-score .filling')['style']
    data['overall_score'] = float(re.search(r'width:([\d.]+)%', overall_score_width).group(1))/20  # Convertir a escala de 5
    
    cost_score_width = city_element.select_one('.rating-cost-score .filling')['style']
    data['cost_score'] = float(re.search(r'width:([\d.]+)%', cost_score_width).group(1))/20
    
    internet_score_width = city_element.select_one('.rating-internet-score .filling')['style']
    data['internet_score'] = float(re.search(r'width:([\d.]+)%', internet_score_width).group(1))/20
    
    like_score_width = city_element.select_one('.rating-like-score .filling')['style']
    data['like_score'] = float(re.search(r'width:([\d.]+)%', like_score_width).group(1))/20

    safety_score_width = city_element.select_one('.rating-safety-score .filling')
    
    safety_score_element = city_element.select_one('.rating-safety-score .filling')
    if 'style' in safety_score_element.attrs and '{rating-safety-score-swidth}' not in safety_score_element['style']:
        safety_score_width = safety_score_element['style']
        data['safety_score'] = float(re.search(r'width:([\d.]+)%', safety_score_width).group(1))/20
    else:
        data['safety_score'] = None
    
    
    # waether
    data['temperature_c'] = float(city_element.select_one('.temperature .unit.metric').text.strip().replace('°', ''))
    data['temperature_f'] = float(city_element.select_one('.temperature .unit.imperial').text.strip().replace('°', ''))
    data['feels_like_c'] = float(city_element.select_one('.label-heat-index .value.unit.metric').text.strip().replace('°', ''))
    data['feels_like_f'] = float(city_element.select_one('.label-heat-index .value.unit.imperial').text.strip().replace('°', ''))
    
    # Humidity
    humidity_element = city_element.select_one('.sweat-emoji')
    data['humidity'] = int(humidity_element['data-humidity']) if humidity_element and 'data-humidity' in humidity_element.attrs else None
    
    # Air quality
    air_quality_element = city_element.select_one('.air_quality .value')
    data['air_quality'] = int(air_quality_element.text.strip()) if air_quality_element else None
    
    # Internet
    internet_speed = city_element.select_one('.mbps').find_previous('span', class_='value')
    data['internet_speed_mbps'] = float(internet_speed.text.strip()) if internet_speed else None
    
    # monthly cost
    price_element = city_element.select_one('.price')
    if price_element:
        price_text = price_element.text.strip()
        data['monthly_cost_usd'] = float(price_text.replace('$', '').replace(',', ''))
        data['monthly_cost_cents'] = int(price_element['data-usd']) if 'data-usd' in price_element.attrs else None
    else:
        data['monthly_cost_usd'] = None
        data['monthly_cost_cents'] = None
    
    # URL of the image
    img_element = city_element.select_one('img.bg')
    if img_element and 'srcset' in img_element.attrs:
        srcset = img_element['srcset']
        first_img_url = srcset.split(' ')[0]
        data['image_url'] = first_img_url
    else:
        data['image_url'] = None
    
    return data

def process_html_file(soup_html) -> List[Dict]:
    """
    Process the HTML file.
    
    Args:
        soup_html: BeautifulSoup object que that contains the HTML content
        
    Returns:
        List of dictionaries with the information of the cities
    """

    city_elements = soup_html.select('li.item[data-type="city"]')
    
    all_cities_data = []
    for city_element in city_elements:
        try:
            city_data = extract_city_data(city_element)
            all_cities_data.append(city_data)
            city_data['city'] = city_data['city'].replace(",", "").lower()
        except Exception as e:
            city_name = city_element.select_one('h2.itemName a')
            city_name = city_name.text.strip() if city_name else "Unknown"
            print(f"Error processing city {city_name}: {e}")
    
    return all_cities_data

def save_to_database(cities_data: List[Dict], output_path: str = 'nomad_cities_database') -> None:
    """
    Keep the extracted data in csv format    
    Args:
        cities_data: List of dictionaries with city data
        output_path: Name of the output file 
    """

    df = pd.DataFrame(cities_data)
    
    df.to_csv(f"{output_path}.csv", index=False)
    print(f"Data Base saved as {output_path}.csv")




if __name__ == "__main__":

    chromedriver_path = 'C:/6Q/HackUPC2025/chromedriver-win64/chromedriver.exe' # path to your chromedriver executable

    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")

    # Create the service with the path to chromedriver
    service = Service(executable_path=chromedriver_path)

    # Create the session 
    driver = webdriver.Chrome(service=service, options=options)

    driver.get("https://nomads.com")
    time.sleep(4)

    # Scroll to load all grid items (you have to make a first scrolling to load the first items)
    SCROLL_PAUSE_TIME = 5
    last_height = driver.execute_script('return document.body.scrollHeight')

    scroll_count = 0

    while True:
        driver.execute_script("window.scrollBy(0, document.body.scrollHeight);")
        time.sleep(SCROLL_PAUSE_TIME)

        new_height = driver.execute_script("return document.body.scrollHeight")
        print(f"Scroll #{scroll_count+1}: Previous height: {last_height}, New height: {new_height}")

        if new_height == last_height:
            break
        last_height = new_height
        scroll_count += 1

    html = driver.page_source
    soup = BeautifulSoup(html, "html.parser")
    all_cities_data = process_html_file(soup)
    save_to_database(all_cities_data, 'nomad_cities_database')
    driver.quit()

    