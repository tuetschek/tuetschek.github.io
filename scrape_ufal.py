#!/usr/bin/env python

import requests
import re
from bs4 import BeautifulSoup


UFAL_SITE = 'https://ufal.mff.cuni.cz/ondrej-dusek'

r = requests.get(UFAL_SITE)
soup = BeautifulSoup(r.content, "lxml")

# scrape students

print("***\nSTUDENTS\n***\n\n")
students = soup.find_all(string=re.compile(r'^\s*Students\s*$'))[0].find_parent().find_next_siblings()[0]  # will get everything in the given div
# get lists of current and former students
current, former = students.find_all('ul')
current = current.find_all('li')
print("\n".join([' ' * 12 + '<li>' + s.decode()[4:-5].strip() + '</li>' for s in current]))
former = former.find_all('li')
print("\n")
print("\n".join([' ' * 12 + '<li>' + s.decode()[4:-5].strip() + '</li>' for s in former]))

# scrape news
print("\n\n***\nNEWS\n***\n\n")
news = soup.find_all(string=re.compile(r'^\s*News\s*$'))[0].find_parent().find_next_siblings()  # will get everything in the given div
bio = soup.find_all(string=re.compile(r'^\s*Biographical\s*$'))[0].find_parent()  # this & below we don't want
news = news[:news.index(bio)]  # get only the news

news = [' ' * 8 + '<li>' + n.decode()[3:-4].strip() + '</li>' for n in news]
print("\n".join(news))


