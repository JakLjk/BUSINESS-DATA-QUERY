import requests
import re
import warnings
import unicodedata
import hashlib
from lxml import etree
from lxml.etree import XMLSyntaxError
from bs4 import BeautifulSoup
from bs4 import XMLParsedAsHTMLWarning
from typing import Dict, List, Tuple
from business_data_api.tasks.exceptions import (
                                            EntityNotFoundException, 
                                            InvalidParameterException,
                                            ScrapingFunctionFailed,
                                            WebpageThrottlingException)
from business_data_api.db import psql_session
from business_data_api.db.models import ScrapedKrsDF


warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


class KRSDokumentyFinansowe():
    """
    Class to handle the retrieval of financial documents from the KRS (Krajowy Rejestr Sądowy).
    """
    KRS_DF_URL = "https://ekrs.ms.gov.pl/rdf/pd/search_df"

    def __init__(self, krs_number):
        self._session = requests.Session()
        self._ajax_headers = {
            "Faces-Request": "partial/ajax",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "Mozilla/5.0",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.KRS_DF_URL,
            "Origin":"https://ekrs.ms.gov.pl"
        }
        self.__krs_number: str = None 
        self.krs_number = krs_number

    @property
    def krs_number(self):
        return self._krs_number
    
    @krs_number.setter
    def krs_number(self, krs_number):
        if not isinstance(krs_number, str):
            raise InvalidParameterException("KRS number must be a string.")
        if len(krs_number) != 10:
            raise InvalidParameterException("KRS number must be exactly 10 digits.")
        if not krs_number.isdigit():
            raise InvalidParameterException("KRS number must contain only digits.")
        self._krs_number = krs_number

    def _request_main_page(self) -> requests.Response:
        response = self._session.get(self.KRS_DF_URL)
        soup = BeautifulSoup(response.text, "html.parser")
        # fetching initial viewstate
        viewstate = soup.find("input", {"name": "javax.faces.ViewState"}).get("value")

        payload = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": "unloggedForm:timeDelBtn",
            "javax.faces.partial.execute": "@all",
            "unloggedForm:timeDelBtn": "unloggedForm:timeDelBtn",
            "unloggedForm": "unloggedForm",
            "unloggedForm:krs0": self.krs_number,
            "javax.faces.ViewState": viewstate
        }
        response = self._session.post(self.KRS_DF_URL, headers=self._ajax_headers, data=payload)
        self._check_exist_documents_for_krs(response)
        self._check_cannot_display_page(response)
        self._check_webpage_throttling(response)
        return response
 
    def _request_page(self, page_num:int, response: requests.Response) -> requests.Response:
        viewstate = self._extract_current_viewstate(response)
        if page_num < 1:
            raise ValueError("Page number must be greater than or equal to 1.")
        first_row = (page_num - 1) * 10
        payload = {
            'javax.faces.partial.ajax': 'true',
            'javax.faces.source': 'searchForm:docTable',
            'javax.faces.partial.execute': 'searchForm:docTable',
            'javax.faces.partial.render': 'searchForm:docTable',
            'searchForm:docTable': 'searchForm:docTable',
            'searchForm:docTable_pagination': 'true',
            'searchForm:docTable_first': first_row,
            'searchForm:docTable_rows': '10',
            'searchForm:docTable_skipChildren': 'true',
            'searchForm:docTable_encodeFeature': 'true',
            'searchForm': 'searchForm',
            'searchForm:j_idt194_focus': '',
            'searchForm:j_idt194_input': '',
            'searchForm:j_idt197_focus': '',
            'searchForm:j_idt197_input': '',
            'searchForm:docTable_rppDD': '10',
            'javax.faces.ViewState': viewstate
        }
        response = self._session.post(self.KRS_DF_URL, headers=self._ajax_headers, data=payload)
        self._check_cannot_display_page(response)
        return response

    def _request_document_details(self, response: requests.Response, details_id:str) -> dict:
        viewstate = self._extract_current_viewstate(response)
        payload = {
            'javax.faces.partial.ajax': 'true',
            'javax.faces.source': details_id,
            'javax.faces.partial.execute': '@all',
            'javax.faces.partial.render': 'searchForm',
            details_id: details_id,
            'searchForm': 'searchForm',
            'searchForm:j_idt194_focus': '',
            'searchForm:j_idt194_input': '',
            'searchForm:j_idt197_focus': '',
            'searchForm:j_idt197_input': '',
            'searchForm:docTable_rppDD': '10',
            'javax.faces.ViewState': viewstate
        }
        response = self._session.post(self.KRS_DF_URL, headers=self._ajax_headers, data=payload)
        self._check_cannot_display_page(response)
        return response

    def _request_pokaz_tresc_dokumentu(self, 
                                        response: requests.Response, 
                                        id_pokaz_tresc_dokumentu: str) -> Tuple[str, requests.Response]:
        viewstate = self._extract_current_viewstate(response)
        payload = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": id_pokaz_tresc_dokumentu,
            "javax.faces.partial.execute": "@all",
            "javax.faces.partial.render": "searchForm",
            id_pokaz_tresc_dokumentu: id_pokaz_tresc_dokumentu,
            "searchForm": "searchForm",
            "searchForm:j_idt194_focus": "",
            "searchForm:j_idt194_input": "",
            "searchForm:j_idt197_focus": "",
            "searchForm:j_idt197_input": "",
            "searchForm:docTable_rppDD": "10",
            "javax.faces.ViewState": viewstate

        }
        response =  self._session.post(self.KRS_DF_URL, headers=self._ajax_headers, data=payload)
        content_disposition = response.headers.get('Content-Disposition')
        if not content_disposition:
            raise ValueError("File name could not be found")
        match = re.search(r'filename="(.+?)"', content_disposition)
        if not match:
            raise ValueError("File name could not be found")
        else:
            filename = match.group(1)
            self._check_file_name_error(filename)
            self._check_cannot_display_page(response)
            return filename, response

    def _extract_current_viewstate(self, response: requests.Response) -> str:
        response_text = response.text
        root = etree.fromstring(response_text.encode())
        viewstate_string = root.xpath('//update[@id="j_id1:javax.faces.ViewState:0"]')
        if not viewstate_string:
            raise ValueError("ViewState not found in the response.")
        return viewstate_string[0].text.strip()

    def _extract_number_of_pages(self, response: requests.Response) -> int:
        response_text = response.text
        root = etree.fromstring(response_text.encode('utf-8'))
        search_form_update_element = root.xpath('.//update[@id="searchForm"]')[0].text
        soup = BeautifulSoup(search_form_update_element, 'html.parser')
        num_of_pages_text = soup.find('span', class_='ui-paginator-current').get_text(strip=True)
        return int(re.search(r'Strona: \s*\d+/(\d+)', num_of_pages_text).group(1))

    def _extract_documents_table_data(self, response: requests.Response) -> list:
        response_text = response.text
        root = etree.fromstring(response_text.encode('utf-8'))
        try:
            search_form_update_element = root.xpath('.//update[@id="searchForm"]')[0].text
        except IndexError:
            search_form_update_element = root.xpath('.//update[@id="searchForm:docTable"]')[0].text
        soup = BeautifulSoup(search_form_update_element, 'html.parser')
        table_soup = soup.find_all('tr')
        if not table_soup:
            raise ValueError("No data table found in the response.")
        table_data = []
        for row in table_soup:
            columns = []
            for cell in row.find_all('td'):
                link = cell.find('a')
                if link and 'Pokaż szczegóły' in link.text:
                    columns.append(link.get('id'))
                else:
                    columns.append(cell.get_text(strip=True))
            table_data.append(columns)
        table_headers = [
            "document_id",
            "document_type",
            "document_name",
            "document_from",
            "document_to",
            "document_status",
            "internal_element_id",
        ]
        table_rows = []
        for row in table_data:
            row_dict = dict(zip(table_headers, row))
            row_dict['document_hash_id'] = self._helper_hash_string(
                self._helper_normalize_string(
                    self.krs_number +
                    row_dict['document_type'] +
                    row_dict['document_name'] +
                    row_dict['document_from'] +
                    row_dict['document_to']
                ))
            table_rows.append(row_dict)
        return table_rows

    def _extract_pokaz_tresc_dokumentu_id(self, response: requests.Response) -> str:
        response_text = response.text
        root = etree.fromstring(response_text.encode('utf-8'))
        element_pokaz_tresc_dokumentu = root.xpath('.//update[@id="searchForm"]')[0].text
        soup = BeautifulSoup(element_pokaz_tresc_dokumentu, 'html.parser')
        return soup.find('a', text='Pokaż treść dokumentu')['id']

    def _helper_normalize_string(self, string:str) -> str:
        return unicodedata.normalize("NFKD", string).strip().lower().replace('\xa0', ' ')

    def _helper_hash_string(self, string:str) ->str:
        return hashlib.sha256(string.encode('UTF-8')).hexdigest()

    def _check_cannot_display_page(self, response: requests.Response) -> bool:
        """Error can appear when stale viewstate was provided"""
        response_text = response.text
        try:
            root = etree.fromstring(response_text.encode('utf-8'))
            viewroot_update = root.xpath('.//update[@id="javax.faces.ViewRoot"]')[0].text
        except IndexError:
            return
        except XMLSyntaxError:
            return
        soup = BeautifulSoup(viewroot_update, 'html.parser')
        
        if 'Witryna sieci Web nie może wyświetlić strony' in soup.get_text():
            raise ScrapingFunctionFailed("\nCould not display page based using injected AJAX function"
            "\nError can arrise when stale viewstate is used"
            )

    def _check_file_name_error(self, filename:str):
        """File name = Error most probably means that the id provided
        by '_extract_pokaz_tresc_dokumentu_id' is incorrect (due to scraping error 
        or webpage structure change)"""
        if 'error' in filename:
            raise ScrapingFunctionFailed(
            "\nFile name = Error most probably means that the id provided"
            "\nby '_extract_pokaz_tresc_dokumentu_id' is incorrect"
            "\n(due to scraping error or webpage structure change)")

    def _check_exist_documents_for_krs(self, response: requests.Response) -> bool:
        response_text = response.text
        try:
            root = etree.fromstring(response_text.encode('utf-8'))
            no_documents_element = root.xpath('.//update[starts-with(@id, "unloggedForm:j_idt")]')[0].text
        except IndexError:
            return
        soup = BeautifulSoup(no_documents_element, 'html.parser')
        if 'Brak dokumentów dla KRS:' in soup.get_text():
            raise EntityNotFoundException("Server Error - No documents for specified KRS")

    def _check_webpage_throttling(self, response: requests.Response):
        response_text = response.text
        try:
            root = etree.fromstring(response_text.encode('utf-8'))
            webpage_throttling_element = root.xpath('.//update[starts-with(@id, "unloggedForm:j_idt")]')[0].text    
        except IndexError:
            return
        soup = BeautifulSoup(webpage_throttling_element, 'html.parser')
        if 'Wymagane oczekiwanie pomiędzy kolejnymi wywołaniami' in soup.get_text():
            raise WebpageThrottlingException("\nWebpage sent throttling error"
                                            "\nBigger intervals between requests may be necessary"
                                            )

    def _save_to_postgresql(self,documents_to_db: List[ScrapedKrsDF]):
        session = psql_session()
        for document in documents_to_db:
            try: 
                record = ScrapedKrsDF(**document)
                session.merge(record)
                session.commit()
            except Exception as e:
                record = ScrapedKrsDF(
                    hash_id = document['hash_id'],
                    status = 'failed',
                    error_message = e
                )
                session.merge(record)
                session.commit
                raise e
            
            finally:
                session.close()

    def get_document_list(self):
        response = self._request_main_page()
        num_pages = self._extract_number_of_pages(response)
        table_data = []
        for n_page in range(1,num_pages+1):
            response = self._request_page(n_page, response)
            table_data.extend(self._extract_documents_table_data(response))
        return table_data

    def download_document(self, document_hash_id_s: str | List):
        if isinstance(document_hash_id_s, str):
            document_hash_id_s = [document_hash_id_s]
        response = self._request_main_page()
        num_pages = self._extract_number_of_pages(response)
        documents_to_db = []
        for n_page in range(1, num_pages + 1):
            response = self._request_page(n_page, response)
            table = self._extract_documents_table_data(response)
            matched_documents = [row for row in table if row['document_hash_id'] in document_hash_id_s]
            for document in matched_documents:
                internal_id = document['document_id']
                hash_id = document['hash_id']
                request_document_details = self._request_document_details(
                                                                    response, 
                                                                    internal_id)
                pokaz_tresc_dokumentu_id = self._extract_pokaz_tresc_dokumentu_id(
                                                                    request_document_details)
                document_save_name, document_data = self._request_pokaz_tresc_dokumentu(
                                                                    request_document_details, 
                                                                    pokaz_tresc_dokumentu_id)
                file_extension = document_name.split('.')[-1]
                
                record = {
                    'hash_id':hash_id,
                    'krs':self.krs_number,
                    'document_internal_id':document['internal_element_id'],
                    'document_type':document['document_type'],
                    'document_name':document['document_name'],
                    'document_date_from':document['document_from'],
                    'document_date_to':document['date_to'],
                    'document_status':document['status'],
                    'content_type':file_extension,
                    'content_content':document_data,
                    'save_name':document_save_name,
                    'status':'success'
                }
                documents_to_db.append(record)
        self._save_to_postgresql(documents_to_db)




def test():
    hash_ids = [
        "a37d56ff51b3a1203c532a1ebe8fbe0a8d6a14ba40b6011fcc8d3dda7935e40e",
        "a1e27a043cea3bedda307715500a50f683917008c4923511e03cfbbe477f9c8a",
        "be22771e67e361fecc4b997fc9836efd7791db74c1e7d4ac987a48ec57928d88"
    ]
    krsdf = KRSDokumentyFinansowe("0000057814")
    # print(krsdf.get_document_list())
    krsdf.download_document(hash_ids)

test()


# TODO add function for checking if KRSDF Is not during maintenance
# TODO postgresql function for flask to fetch statuses of each document being scraped
# TODO addiitonal check - we check if documents send to worker are scrpaed (done status in psotgresql), and if
# not yet scrpaed we make sure that the worker process is still running
