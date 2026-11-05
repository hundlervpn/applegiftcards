from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.openapi import UNAUTHORIZED_RESPONSE, error_response
from app.db.session import get_db
from app.dependencies import CurrentUser
from app.schemas.support_ticket import (
    SupportTicketCreate,
    SupportTicketListResponse,
    SupportTicketResponse,
)
from app.services.alerts import notify_support_ticket
from app.services.support_tickets import (
    SupportOrderNotFound,
    SupportTicketsUnavailable,
    create_ticket,
    list_user_tickets,
)

router = APIRouter(prefix="/support/tickets", tags=["support"])

UNAVAILABLE_RESPONSE = error_response(
    "Хранилище обращений временно недоступно.", unavailable="Support tickets are unavailable"
)


@router.post(
    "",
    status_code=201,
    response_model=SupportTicketResponse,
    summary="Создать обращение в поддержку",
    description=(
        "Создаёт обращение со статусом open от владельца JWT. "
        "Тема содержит 5–120 символов, описание — 10–2000 символов после удаления пробелов по краям. "
        "Заказ необязателен; чужой и несуществующий заказ одинаково возвращают 404. "
        "После сохранения фоновая задача уведомляет администратора через Telegram, если он настроен."
    ),
    responses={
        401: UNAUTHORIZED_RESPONSE,
        404: error_response("Собственный заказ не найден.", missing="Order not found"),
        503: UNAVAILABLE_RESPONSE,
    },
)
async def post_ticket(
    body: SupportTicketCreate,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> SupportTicketResponse:
    try:
        ticket = await create_ticket(session, user.id, body)
    except SupportOrderNotFound as exc:
        raise HTTPException(status_code=404, detail="Order not found") from exc
    except SupportTicketsUnavailable as exc:
        raise HTTPException(status_code=503, detail="Support tickets are unavailable") from exc
    background_tasks.add_task(
        notify_support_ticket, ticket.id, user.email, ticket.subject, ticket.order_id
    )
    return ticket


@router.get(
    "",
    response_model=SupportTicketListResponse,
    summary="Список своих обращений",
    description=(
        "Возвращает только обращения владельца JWT, от новых к старым, "
        "включая текущий статус, тему, текст и связанный заказ. "
        "Параметр user_id не используется; total не включает чужие обращения."
    ),
    responses={401: UNAUTHORIZED_RESPONSE, 503: UNAVAILABLE_RESPONSE},
)
async def get_tickets(
    user: CurrentUser,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> SupportTicketListResponse:
    try:
        return await list_user_tickets(session, user.id, limit, offset)
    except SupportTicketsUnavailable as exc:
        raise HTTPException(status_code=503, detail="Support tickets are unavailable") from exc
