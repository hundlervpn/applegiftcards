import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.support_ticket import SupportTicket
from app.repositories import support_tickets as tickets_repo
from app.schemas.support_ticket import (
    SupportTicketCreate,
    SupportTicketListResponse,
    SupportTicketResponse,
)

logger = logging.getLogger(__name__)


class SupportOrderNotFound(Exception):
    pass


class SupportTicketsUnavailable(Exception):
    pass


async def create_ticket(
    session: AsyncSession, user_id: int, body: SupportTicketCreate
) -> SupportTicketResponse:
    try:
        if body.order_id is not None and not await tickets_repo.owns_order(
            session, user_id, body.order_id
        ):
            raise SupportOrderNotFound("Order not found")
        ticket = await tickets_repo.add(
            session,
            SupportTicket(user_id=user_id, **body.model_dump()),
        )
        response = SupportTicketResponse.model_validate(ticket)
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        logger.warning("Support ticket creation failed (%s)", type(exc).__name__)
        raise SupportTicketsUnavailable("Could not save support ticket") from exc
    return response


async def list_user_tickets(
    session: AsyncSession, user_id: int, limit: int, offset: int
) -> SupportTicketListResponse:
    try:
        tickets = await tickets_repo.list_for_user(session, user_id, limit, offset)
        total = await tickets_repo.count_for_user(session, user_id)
        return SupportTicketListResponse(
            tickets=[SupportTicketResponse.model_validate(ticket) for ticket in tickets],
            total=total,
        )
    except SQLAlchemyError as exc:
        logger.warning("Support ticket listing failed (%s)", type(exc).__name__)
        raise SupportTicketsUnavailable("Could not read support tickets") from exc
