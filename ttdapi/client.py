import requests
import re
from urllib.parse import urljoin
from typing import Iterator, Tuple, Dict, Optional
import logging

from ttdapi.exceptions import TTDApiPermissionsError, TTDApiError

logger = logging.getLogger(__name__)


class BaseTTDClient(requests.Session):
    """
    The TDD Client can
    - authenticate with username/password and get a token.
    -  cache existing tokens and seamlessly get new ones
    -  perform authenticated requests
    - [TODO] implements retry policy on 500
    - reuses underlying TCP connection for better performance


    The authentication headers are automatically used for each request,
    they are updated whenever a access_token is set


    Do not use this client for any nonthetradedesk urls otherwise your
    token will be exposed

    """

    def __init__(self, login, password,
                 token_expires_in=90,
                 base_url="https://api.thetradedesk.com/v3/"):
        super().__init__()
        self.base_url = base_url
        self._login = login
        self._password = password
        self.token_expires_in = token_expires_in

        self._token = None

    def _build_url(self, endpoint):
        return urljoin(self.base_url, endpoint.lstrip("/"))

    @property
    def token(self):
        """Get or reuse token"""
        if self._token is None:
            self._refresh_token(self.token_expires_in)
        return self._token

    @token.setter
    def token(self, new_token):
        """Set access token + update auth headers"""
        logger.debug("Setting new access token")
        self._token = new_token

        logger.debug("Updating TTD-Auth header")
        self.headers.update({"TTD-Auth": new_token})

    def _refresh_token(self, expires_in=90):

        logger.debug("Getting new access token")
        data = {
            "Login": self._login,
            "Password": self._password,
            "TokenExpirationInMinutes": expires_in
        }

        resp = self.request("POST", self._build_url('authentication'), json=data)
        try:
            resp.raise_for_status()
        except requests.HTTPError as err:
            if err.response.status_code in (401, 403):
                raise TTDApiPermissionsError("{}\n{}".format(err, err.response.text), response=err.response)
            else:
                raise TTDApiError(err.response.text, response=err.response)

        self.token = resp.json()["Token"]

    def _request(self, method, url, *args, **kwargs):
        """Do authenticated HTTP request

        Implements retry on expired access token

        Returns:
            requests.Response object
        """

        # this gets the token if one doesn't already exist
        # it's smelly but should do fine for now

        # auth headers are set when requesting token
        resp = self.request(method, url, *args, **kwargs)
        try:
            resp.raise_for_status()
        except requests.HTTPError as err:
            if err.response.status_code in (401, 403):
                # token expired
                logger.debug("Token expired or invalid, trying again")
                self._refresh_token()
                try:
                    resp2 = self.request(method, url, *args, **kwargs)
                    resp2.raise_for_status()
                except requests.HTTPError as err:
                    raise TTDApiError("{}\n{}".format(err, err.response.text), response=err.response)
                else:
                    return resp2
            else:
                raise TTDApiError("{}\n{}".format(err, err.response.text), response=err.response)
        else:
            return resp

    def get(self, endpoint, *args, **kwargs):
        """Make an authenticated get request against the API endpoint

        Args:
            standard requests.get() parameters

        Returns:
            json response
        """
        return self._request("GET", self._build_url(endpoint), *args, **kwargs).json()

    def post_paginated(self,
                       endpoint,
                       json_payload,
                       page_start_index=0,
                       page_size=100,
                       stream_items=False,
                       **kwargs):
        """Make a POST request and paginate to the end

        Supply the json payload (as a dict) in the "json" argument
        Pagination starts at 0 with page size of 100 if not specified in the json argument

        """
        paging_params = {
            "PageStartIndex": page_start_index,
            "PageSize": page_size
        }
        json_payload.update(paging_params)
        while True:
            resp = self.post(endpoint, json=json_payload, **kwargs)
            # for backward compatibility
            if stream_items:
                for item in resp['Result']:
                    yield item
            else:
                yield resp
            if len(resp['Result']) < page_size:
                break
            else:
                paging_params['PageStartIndex'] += page_size
                json_payload.update(paging_params)


    def post(self, endpoint, *args, **kwargs):
        """Make an authenticated POST request against the API endpoint

        Args:
            standard requests.post() parameters

        Returns:
            json response
        """
        return self._request("POST", self._build_url(endpoint), *args, **kwargs).json()

    def put(self, endpoint, *args, **kwargs):
        """Make an authenticated POST request against the API endpoint

        Args:
            standard requests.put() parameters

        Returns:
            json response
        """

        return self._request("PUT", self._build_url(endpoint), *args, **kwargs).json()



class TTDClient(BaseTTDClient):
    """
    Contains shortcut methods for some endpoints (create_campaign, etc...)

    """
    def create_campaign(self, data):
        return self.post('/campaign', json=data)

    def create_adgroup(self, data):
        return self.post('/adgroup', json=data)

    def update_adgroup(self, data):
        return self.post('/adgroup', json=data)

    def update_campaign(self, data):
        return self.put('/campaign', json=data)

    def get_sitelist(self, id_):
        return self.get('/sitelist/{}'.format(id_))

    def get_all_sitelists(self, json_payload, **paging_params):
        """Get all sitelists for given advertiser

        Args:
            json_data: all parameters you wish to pass.
                If you do not specify the pagination parameters, it starts from
                zero with 100 items per page
                For example {"AdvertiserId": "abcderf"} would do just fine
            paging_params any of "page_start_index", "page_size"

        """

        yield from self.post_paginated("/sitelist/query/advertiser",
                                       json_payload=json_payload,
                                        stream_items=True,
                                       **paging_params)

    def get_all_advertisers(self, json_payload, **paging_params):
        yield from self.post_paginated("/advertiser/query/partner",
                                        json_payload=json_payload,
                                        stream_items=True,
                                        **paging_params)


    def get_campaign_template(self, campaign_id):
        return self.get("campaign/template/{}".format(campaign_id))

    def get_delta_sitelists(self, data):
        "https://apisb.thetradedesk.com/v3/doc/api/post-delta-sitelist-query-advertiser"
        yield from self.post_paginated("/delta/sitelist/query/advertiser", json_payload=data)

    def _post_delta_endpoint(
            self,
            endpoint,
            json_payload,
            entity: str
    ) -> Tuple[Iterator[dict],
               int,
               bool]:
        """Make a request to get data from delta endpoint


        Returns:
            a tuple,
            0 - the Iterator[dict] are the requested items
            1 - the int is new lastChangeTrackingVersion
            2 - bool if there are more items to download, just make a new request with [1]
        """

        resp = self.post(endpoint, json=json_payload)

        return iter(resp[entity]), resp['LastChangeTrackingVersion'], resp['More{entity}Available'.format(entity=entity)]

    def _fetch_one_delta_campaign_for_advertiser(
            self,
            advertiser_id,
            last_change_tracking_version):
        '''Just one "page" '''

        payload = {
            "AdvertiserId": advertiser_id,
            "ReturnEntireCampaign": True,
            "LastChangeTrackingVersion": last_change_tracking_version

        }

        return self._post_delta_endpoint(
            "delta/campaign/query/advertiser",
            json_payload=payload,
            entity='Campaigns')

    def fetch_all_delta_campaigns_for_advertiser(
            self,
            advertiser_id,
            last_change_tracking_version: Optional[int]=None):
        '''Handle "pagination"'''

        campaigns, new_tracking_version, more_data = self._fetch_one_delta_campaign_for_advertiser(
            advertiser_id,
            last_change_tracking_version)

        # if there are no items to be iterated over
        # we still need to yield the tracking version
        # because it must be cached to statefile
        try:
            yield next(campaigns), new_tracking_version
        except StopIteration:
            yield None, new_tracking_version
        else:
            for campaign in campaigns:
                yield campaign, new_tracking_version

        while more_data:
            # a pseudo recursive call
            # because now one can do
            # for campaign, tracking_version in fetch_all_delta_campaigns_for_advertiser(...):
            #
            #to iterate smoothly over the results
            campaigns, new_tracking_version, more_data = self._fetch_one_delta_campaign_for_advertiser(
                advertiser_id,
                new_tracking_version)

            for campaign in campaigns:
                yield campaign, new_tracking_version


    def _fetch_one_delta_adgroup_for_advertiser(
            self,
            advertiser_id,
            last_change_tracking_version):
        '''Just one "page" '''

        payload = {
            "AdvertiserId": advertiser_id,
            "ReturnEntireAdgroup": True,
            "LastChangeTrackingVersion": last_change_tracking_version

        }

        return self._post_delta_endpoint(
            "delta/adgroup/query/advertiser",
            json_payload=payload,
            entity='AdGroups')

    def fetch_all_delta_adgroups_for_advertiser(
            self,
            advertiser_id,
            last_change_tracking_version: Optional[int]=None):
        """Handle 'pagination' """

        adgroups, new_tracking_version, more_data = self._fetch_one_delta_adgroup_for_advertiser(
            advertiser_id,
            last_change_tracking_version)

        # if there are no items to be iterated over
        # we still need to yield the tracking version
        # because it must be cached to statefile
        try:
            yield next(adgroups), new_tracking_version
        except StopIteration:
            yield None, new_tracking_version
        else:
            for adgroup in adgroups:
                yield adgroup, new_tracking_version

        while more_data:
            # a pseudo recursive call
            # because now one can do
            # for adgroup, tracking_version in fetch_all_delta_adgroups_for_advertiser(...):
            #
            #to iterate smoothly over the results
            adgroups, new_tracking_version, more_data = self._fetch_one_delta_adgroup_for_advertiser(
                advertiser_id,
                new_tracking_version)

            for adgroup in adgroups:
                yield adgroup, new_tracking_version
