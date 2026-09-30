"""Небольшие ссылки пагинации с сохранением остальных фильтров."""


def page_url(request, parameter, number):
    query = request.GET.copy()
    query[parameter] = str(number)
    return f"{request.path}?{query.urlencode()}"


def page_links(request, page, parameter="page"):
    return {
        "previous_url": page_url(request, parameter, page.previous_page_number()) if page.has_previous() else "",
        "next_url": page_url(request, parameter, page.next_page_number()) if page.has_next() else "",
    }
