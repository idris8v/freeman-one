from test_bot import database as database,register
from test_tickets import config
from app.database.repositories import SQLRepository
from app.services.screens import ScreenService
from app.services.domain import SubscriptionService
from app.services.integrations import MockPaymentService,MockProvisioningService
