"""Email Management: what an administrator may set, and what is read back.

The CC policy is one setting for the deployment, like the standing CC it replaced:
a list of addresses, each with the emails it is copied on. It is a *replacement*
(the whole list is sent), so removing an address is leaving it out and nothing has
to be remembered between requests.

Addresses go through the shared `Email` rule and every email key is checked against
the catalogue (`app/core/email_catalogue.py`), so a policy can never name an email
that does not exist or one that must never be copied.
"""
from datetime import datetime
from typing import Annotated, List, Optional

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

from app.core.email_catalogue import MAX_CC_ADDRESSES, cc_allowed_keys, trigger_keys
from app.core.validation import Email


#: The shared `Email` rule lower-cases only the domain. The sender lower-cases the whole
#: address (`provider.normalise_address`), so the policy stores it that way too: "A@x.com"
#: and "a@x.com" are one address, listed once.
CcAddress = Annotated[Email, AfterValidator(lambda value: value.lower())]


class EmailCcAddressIn(BaseModel):
    email: CcAddress
    #: The email keys this address is copied on. Anything not listed is not copied.
    triggers: List[str] = Field(default_factory=list, max_length=len(trigger_keys()))

    @field_validator("triggers")
    @classmethod
    def triggers_are_known_and_copyable(cls, value: List[str]) -> List[str]:
        seen: List[str] = []
        known, copyable = trigger_keys(), cc_allowed_keys()
        for key in value:
            if key not in known:
                raise ValueError(f"'{key}' is not one of Monitra's emails.")
            if key not in copyable:
                raise ValueError(
                    f"'{key}' carries a private link and is never copied to anyone else."
                )
            if key not in seen:
                seen.append(key)
        return seen


class EmailCcUpdate(BaseModel):
    addresses: List[EmailCcAddressIn] = Field(max_length=MAX_CC_ADDRESSES)
    #: The `version` the administrator was looking at when they saved. When it is
    #: sent and is no longer the stored version, the save is refused (409) rather
    #: than silently discarding what another administrator saved in between.
    version: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def addresses_are_unique(self):
        emails = [address.email for address in self.addresses]
        if len(set(emails)) != len(emails):
            raise ValueError("Each CC address may be listed once.")
        return self


class EmailTriggerRead(BaseModel):
    key: str
    number: int
    name: str
    goes_to: str
    trigger: str
    cc_allowed: bool
    note: Optional[str] = None
    #: False while the whole email is switched off in the deployment's
    #: configuration -- it is still listed, and still configurable.
    active: bool = True


class EmailCcAddressRead(BaseModel):
    email: str
    triggers: List[str]


class EmailCcRead(BaseModel):
    triggers: List[EmailTriggerRead]
    addresses: List[EmailCcAddressRead]
    max_addresses: int
    #: True until an administrator saves: the addresses are then the deployment's
    #: configured standing CC, copied on every email that may be copied.
    is_default: bool
    version: int
    updated_at: Optional[datetime] = None
    updated_by_username: Optional[str] = None
