# -*- coding: utf-8 -*-

""" Online Payment Services Integration

    @copyright: (c) 2020-2020 Sahana Software Foundation
    @license: MIT

    Permission is hereby granted, free of charge, to any person
    obtaining a copy of this software and associated documentation
    files (the "Software"), to deal in the Software without
    restriction, including without limitation the rights to use,
    copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the
    Software is furnished to do so, subject to the following
    conditions:

    The above copyright notice and this permission notice shall be
    included in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
    EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
    OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
    NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
    HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
    WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
    FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
    OTHER DEALINGS IN THE SOFTWARE.
"""

__all__ = ("S3Payments",
           "S3PaymentService",
           )

import datetime
import json
import sys

from gluon import current

from s3compat import PY2, HTTPError, URLError, urlencode, urllib2
from ..s3rest import S3Method
from ..s3utils import s3_str
from ..s3validators import JSONERRORS

# =============================================================================
class S3Payments(S3Method):
    """ REST Methods to interact with online payment services """

    # -------------------------------------------------------------------------
    def apply_method(self, r, **attr):
        """
            Page-render entry point for REST interface.

            @param r: the S3Request instance
            @param attr: controller attributes
        """

        output = {}
        if r.http == "GET":
            method = r.method
            if method == "confirm":
                output = self.confirm_subscription(r, **attr)
            elif method == "cancel":
                output = self.cancel_subscription(r, **attr)
            else:
                r.error(405, current.ERROR.BAD_METHOD)
        else:
            r.error(405, current.ERROR.BAD_METHOD)

        return output

    # -------------------------------------------------------------------------
    def confirm_subscription(self, r, **attr):
        """
            Check subscription status and trigger automated fulfillment

            @param r: the S3Request instance
            @param attr: controller attributes
        """

        # Validate parameters
        record = r.record
        if not record or not record.service_id:
            r.error(405, "Invalid record")
        try:
            adapter = S3PaymentService.adapter(record.service_id)
        except (KeyError, ValueError):
            r.error(405, "Invalid payment service")
        if not adapter.verify_reference(r):
            r.error(405, "Invalid reference")

        T = current.T

        # Check current status of the subscription
        status = adapter.check_subscription(record.id)
        if status == "ACTIVE":
            # TODO redirect to fulfillment page
            current.response.confirmation = T("Subscription activated")
        elif status == "APPROVED":
            # TODO:
            # - try to activate subscription
            # - on success: redirect to fulfillment page
            # - on failure: show error
            pass
        elif status:
            # TODO handle other statuses => show error/warning
            current.response.warning = T("Subscription inactive")
        else:
            current.response.error = T("Could not verify subscription status")

        r.http = "POST"
        self.next = r.url(method="")

        return {}

    # -------------------------------------------------------------------------
    def cancel_subscription(self, r, **attr):
        """
            Check subscription status and trigger automated cancelation

            @param r: the S3Request instance
            @param attr: controller attributes
        """

        T = current.T

        record = r.record
        if not record or not record.service_id:
            r.error(405, "Invalid record")

        try:
            adapter = S3PaymentService.adapter(record.service_id)
        except (KeyError, ValueError):
            r.error(405, "Invalid payment service")
        if not adapter.verify_reference(r):
            r.error(405, "Invalid reference")

        # TODO Dialog to confirm cancellation

        if adapter.cancel_subscription(record.id):
            current.response.confirmation = T("Subscription cancelled")
        else:
            r.error()

        r.http = "POST"
        self.next = r.url(method="")

        return {}

# =============================================================================
class S3PaymentLog(object):
    """
        Simple log writer for Payment Service Adapters
    """

    def __init__(self, service_id):
        """
            Constructor

            @param service_id: the fin_payment_service record ID
        """
        self.service_id = service_id

    # -------------------------------------------------------------------------
    def write(self, action, result, reason):
        """
            Add a log entry

            @param action: description of the attempted action
            @param result: result of the action
            @param reason: reason for the result (e.g. error message)

            NB: Logging process must either explicitly commit before
                raising an exception, or catch+resolve all exceptions,
                otherwise the log entries will be rolled back
        """

        if not action or not result:
            return

        table = current.s3db.fin_payment_log
        table.insert(date = datetime.datetime.utcnow(),
                     service_id = self.service_id,
                     action = action,
                     result = result,
                     reason = reason,
                     )

        if result in ("ERROR", "FATAL"):
            # Support debugging by additionally logging critical
            # errors in log file (if enabled), in case a subsequent
            # uncaught exception would roll back the DB log entry
            if reason:
                msg = "'%s' failed [%s]" % (action, reason)
            else:
                msg = "'%s' failed" % action
            current.log.error("Payment Service #%s: %s" % (self.service_id, msg))

    # -------------------------------------------------------------------------
    # Shortcuts
    #
    def info(self, action, reason=None):
        self.write(action, "INFO", reason)
    def success(self, action, reason=None):
        self.write(action, "SUCCESS", reason)
    def warning(self, action, reason=None):
        self.write(action, "WARNING", reason)
    def error(self, action, reason=None):
        self.write(action, "ERROR", reason)
    def fatal(self, action, reason=None):
        self.write(action, "FATAL", reason)

# =============================================================================
class S3PaymentService(object):
    """ Online Payment Service (base class) """

    def __init__(self, row):
        """
            Constructor

            @param row: the fin_payment_service Row
        """

        # Store service config record ID
        self.service_id = row.id

        # Instantiate log writer
        self.log = S3PaymentLog(self.service_id)

        # Store attributes
        self.base_url = row.base_url
        self.use_proxy = row.use_proxy
        self.proxy = row.proxy

        self.username = row.username
        self.password = row.password

        self.token_type = row.token_type
        self.access_token = row.access_token
        self.token_expiry_date = row.token_expiry_date

    # -------------------------------------------------------------------------
    # API Functions (to be implemented by subclasses)
    # -------------------------------------------------------------------------
    def get_access_token(self):
        """
            Retrieve and store a new access token for Token-Auth using
            current username (=client_id) and password (=client_secret);
            to be called implicitly by http() if/when required

            @returns: the new access token if successful, otherwise None
        """
        raise NotImplementedError

    # -------------------------------------------------------------------------
    def get_user_info(self):
        """
            Retrieve user information from this service; for account
            verification and API testing (implementation optional)

            @returns: a dict with user details
        """
        raise NotImplementedError

    # -------------------------------------------------------------------------
    def register_product(self, product_id):
        """
            Register a product with this payment service

            @param product_id: the fin_product record ID

            @returns: True if successful, or False on error
        """
        raise NotImplementedError

    # -------------------------------------------------------------------------
    def update_product(self, product_id):
        """
            Update a product registration in this payment service

            @param product_id: the fin_product record ID

            @returns: True if successful, or False on error
        """
        raise NotImplementedError

    # -------------------------------------------------------------------------
    def retire_product(self, product_id):
        """
            Retire a product from this payment service

            @param product_id: the fin_product record ID

            @returns: True if successful, or False on error
        """
        raise NotImplementedError

    # -------------------------------------------------------------------------
    def register_subscription_plan(self, plan_id):
        """
            Register a subscription plan with this service

            @param plan_id: the fin_subscription_plan record ID

            @returns: True if successful, or False on error
        """
        raise NotImplementedError

    # -------------------------------------------------------------------------
    def update_subscription_plan(self, plan_id):
        """
            Update a subscription plan

            @param plan_id: the fin_subscription_plan record ID

            @returns: True if successful, or False on error
        """
        raise NotImplementedError

    # -------------------------------------------------------------------------
    def register_subscription(self, plan_id, pe_id):
        """
            Register a subscription with this service

            @param plan_id: the subscription plan ID
            @param pe_id: the subscriber PE ID

            @returns: the record ID of the newly created subscription
        """
        raise NotImplementedError

    # -------------------------------------------------------------------------
    def check_subscription(self, subscription_id):
        """
            Check the current status of a subscription (and update it
            in the database)

            @param subscription_id: the subscription record ID

            @returns: the current status of the subscription, or None on error
        """
        raise NotImplementedError

    # -------------------------------------------------------------------------
    def activate_subscription(self, subscription_id):
        """
            Activate a subscription (on the service side)

            @param subscription_id: the subscription record ID

            @returns: True if successful, or False on error
        """
        raise NotImplementedError

    # -------------------------------------------------------------------------
    def cancel_subscription(self, subscription_id):
        """
            Cancel a subscription

            @param subscription_id: the subscription record ID

            @returns: True if successful, or False on error
        """
        raise NotImplementedError

    # -------------------------------------------------------------------------
    @staticmethod
    def verify_reference(r):
        """
            Verify the reference number of a subscription specified
            in the URL; subclasses may override to use a different
            query variable, or not require a match at all

            @param r: the S3Request
            @returns: True|False whether the specified reference matches
                      the requested subscription record
        """

        record = r.record
        if not record or not record.refno:
            return False

        refno = r.get_vars.get("subscription_id")

        return refno == record.refno

    # -------------------------------------------------------------------------
    # Utility Functions
    # -------------------------------------------------------------------------
    def http(self,
             method = "GET",
             path = None,
             args = None,
             data = None,
             headers = None,
             auth = False,
             encode = "json",
             decode = "json",
             ):
        """
            Send a HTTP request to the REST API

            @param method: the HTTP method
            @param path: the path relative to the base URL of the service
            @param args: dict of URL parameters
            @param data: data to send (JSON-serializable object)
            @param auth: False - do not send an Auth header
                         "Basic" - send an Auth header with username+password
                         "Token" - send an Auth header with access token
            @param encode: encode the request data as "json"|"text"|"bytes"
            @param decode: decode the response as "json"|"text"|"bytes"
        """

        base_url = self.base_url
        if not base_url:
            return None, None, "No base URL set"

        url = base_url.rstrip("/")
        if path:
            url = "/".join((url, path.lstrip("/")))
        if args:
            url = "?".join((url, urlencode(args)))

        # Generate the Request
        if PY2:
            req = urllib2.Request(url)
            req.get_method = lambda: method
        else:
            req = urllib2.Request(url = url, method = method)

        # Request data
        if method == "GET":
            request_data = None
        elif data:
            if encode == "json":
                request_data = json.dumps(data).encode("utf-8")
                req.add_header("Content-Type", "application/json")
            elif encode == "text":
                request_data = s3_str(data).encode("utf-8")
                req.add_header("Content-Type", "text/plain; charset=UTF-8")
            else:
                request_data = s3_str(data).encode("utf-8")
                req.add_header("Content-Type", "application/octet-stream")
        else:
            request_data = None

        # Acceptable response type
        if decode == "json":
            req.add_header("Accept", "application/json")
        else:
            req.add_header("Accept", "*/*")

        # Run the request
        output = status = error = None

        opener = self._http_opener(req, headers=headers, auth=auth)
        try:
            f = opener.open(req, data=request_data)
        except HTTPError as e:
            # HTTP error status
            status = e.code
            error = s3_str(e.read())
        except URLError as e:
            # Network Error
            error = e.reason
            if not error:
                error = "Unknown network error"
        except Exception:
            # Other Error
            error = sys.exc_info()[1]
        else:
            status = f.code

            # Decode the response
            if decode == "json":
                try:
                    output = json.load(f)
                except JSONERRORS:
                    error = sys.exc_info()[1]
            elif decode == "text":
                output = s3_str(f.read())
            else:
                # return the stream as-is
                output = f

        return output, status, error

    # -------------------------------------------------------------------------
    def _http_opener(self, req, headers=None, auth=True):
        """
            Configure a HTTP opener for API operations

            @param req: the Request
            @param headers: array of HTTP headers
            @param auth: False - do not add Authorization
                         "Basic" - add Auth header using username+password
                         "Token" - add Auth header using access token
                         any tru-ish value - add a 401-handler with username+password

            @returns: OpenerDirector instance
        """

        # Configure opener headers
        addheaders = []
        if headers:
            addheaders.extend(headers)

        # Configure opener handlers
        handlers = []

        # Proxy handling
        proxy = self.proxy
        if proxy and self.use_proxy:
            protocol = req.get_type() if PY2 else req.type
            proxy_handler = urllib2.ProxyHandler({protocol: proxy})
            handlers.append(proxy_handler)

        # Authentication handling
        username = self.username
        password = self.password

        if auth == "Basic":
            if username and password:
                import base64
                base64string = base64.b64encode(('%s:%s' % (username, password)).encode("utf-8"))
                addheaders.append(("Authorization", "Basic %s" % s3_str(base64string)))

        elif auth == "Token":
            token = self.access_token
            token_type = self.token_type or "Bearer"
            expiry_date = self.token_expiry_date
            if not token or \
               expiry_date and expiry_date <= current.request.utcnow:
                try:
                    token = self.get_access_token()
                except NotImplementedError:
                    token = None
            if token:
                addheaders.append(("Authorization", "%s %s" % (token_type, token)))

        else:
            # No pre-emptive auth
            pass

        if auth and username and password:
            # Add a HTTP-401-handler as fallback in case pre-emptive auth fails
            passwd_manager = urllib2.HTTPPasswordMgrWithDefaultRealm()
            passwd_manager.add_password(realm = None,
                                        uri = req.get_full_url(),
                                        user = username,
                                        passwd = password,
                                        )
            auth_handler = urllib2.HTTPBasicAuthHandler(passwd_manager)
            handlers.append(auth_handler)

        # Create the opener and add headers
        opener = urllib2.build_opener(*handlers)
        if addheaders:
            opener.addheaders = addheaders

        return opener

    # -------------------------------------------------------------------------
    def has_product(self, product_id):
        """
            Check whether a product is already registered with this service

            @param product_id: the fin_product record ID

            @returns: boolean
        """

        table = current.s3db.fin_product_service
        query = (table.product_id == product_id) & \
                (table.service_id == self.service_id) & \
                (table.deleted == False)

        row = current.db(query).select(table.is_registered,
                                       limitby = (0, 1),
                                       ).first()

        return row.is_registered if row else False

    # -------------------------------------------------------------------------
    def has_subscription_plan(self, plan_id):
        """
            Check whether a subscription_plan is already registered
            with this service

            @param plan_id: the fin_subscription_plan record ID

            @returns: boolean
        """

        table = current.s3db.fin_subscription_plan_service
        query = (table.plan_id == plan_id) & \
                (table.service_id == self.service_id) & \
                (table.deleted == False)

        row = current.db(query).select(table.is_registered,
                                       limitby = (0, 1),
                                       ).first()

        return row.is_registered if row else False

    # -------------------------------------------------------------------------
    @staticmethod
    def get_subscriber_info(pe_id):
        """
            Retrieve information about the subscriber from the DB

            @param pe_id: the PE ID of the subscriber

            @returns: a tuple (info, error), where info is a dict like:
                        {"first_name": first or only name
                         "last_name":  last name
                         "email":      email address
                         }
        """

        db = current.db
        s3db = current.s3db

        subscriber = {}

        # Look up subscriber type
        petable = s3db.pr_pentity
        query = (petable.pe_id == pe_id) & \
                (petable.deleted == False)
        entity = db(query).select(petable.instance_type,
                                  limitby = (0, 1),
                                  ).first()
        if not entity:
            return None, "Unknown subscriber #%s" % pe_id

        subscriber_type = entity.instance_type
        etable = s3db.table(subscriber_type)
        if not etable:
            return None, "Unknown subscriber type"

        # Look up subscriber name
        query = (etable.pe_id == pe_id) & \
                (etable.deleted == False)
        if subscriber_type == "org_organisation":
            row = db(query).select(etable.name,
                                   limitby = (0, 1),
                                   ).first()
            subscriber["first_name"] = row.name
        elif subscriber_type == "pr_person":
            row = db(query).select(etable.first_name,
                                   etable.last_name,
                                   limitby = (0, 1),
                                   ).first()
            subscriber["first_name"] = row.first_name
            subscriber["last_name"] = row.last_name
        else:
            return None, "Invalid subscriber type %s" % subscriber_type

        # Look up subscriber email-address
        ctable = s3db.pr_contact
        query = (ctable.pe_id == pe_id) & \
                (ctable.contact_method == "EMAIL") & \
                (ctable.deleted == False)
        # If the user can differentiate between public and private
        # email addresses, then exclude the private ones
        setting = current.deployment_settings.get_pr_contacts_tabs()
        if "private" in setting:
            query &= ((ctable.access == 2) | (ctable.access == None))

        row = db(query).select(ctable.value,
                               orderby = ctable.priority,
                               limitby = (0, 1),
                               ).first()
        if row:
            subscriber["email"] = row.value

        return subscriber, None

    # -------------------------------------------------------------------------
    @staticmethod
    def get_merchant_name(product_id):
        """
            Get the merchant name (org name) for a product

            @param product_id: the product ID

            @returns: the name as string, or None if not available
        """

        db = current.db
        s3db = current.s3db

        ptable = s3db.fin_product
        otable = s3db.org_organisation
        query = (ptable.id == product_id) & \
                (otable.id == ptable.organisation_id) & \
                (ptable.deleted == False)
        row = db(query).select(otable.name,
                               limitby = (0, 1),
                               ).first()

        return row.name if row else None

    # -------------------------------------------------------------------------
    # Factory Method
    # -------------------------------------------------------------------------
    @staticmethod
    def adapter(service_id):
        """
            Instantiate and return a suitable API adapter for a payment
            service

            @param service_id: the fin_payment_service record ID
        """

        # Load service configuration
        # - resource.select implies READ permission required,
        #   so access to service config can be restricted to
        #   certain user roles and realms
        resource = current.s3db.resource("fin_payment_service",
                                         id = service_id,
                                         )
        rows = resource.select(["id",
                                "api_type",
                                "base_url",
                                "use_proxy",
                                "proxy",
                                "username",
                                "password",
                                "token_type",
                                "access_token",
                                "token_expiry_date",
                                ],
                                limit = 1,
                                as_rows = True,
                                )
        if not rows:
            raise KeyError("Payment service configuration #%s not found" % service_id)
        row = rows[0]

        # All adapters
        from .paypal import PayPalAdapter

        adapters = {"PAYPAL": PayPalAdapter,
                    }

        # Get adapter for API type
        adapter = adapters.get(row.api_type)
        if not adapter:
            raise ValueError("Invalid API type: %s" % row.api_type)

        # Instantiate adapter with Row and return it
        return adapter(row)

# END =========================================================================
