from rest_framework.response import Response


def paginated_response(queryset, serializer_class, *, page, page_size):
    """Serialize one page using Dazzy's standard list response envelope."""
    total = queryset.count()
    items = queryset[(page - 1) * page_size : page * page_size]
    return Response(
        {
            "data": {
                "items": serializer_class(items, many=True).data,
                "pagination": {"page": page, "page_size": page_size, "total": total},
            }
        }
    )
