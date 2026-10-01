from django.urls import path
from . import views

app_name = 'dialogues'
urlpatterns = [
    path('', views.index, name='index'),
    path('start/<int:scenario_id>/', views.start, name='start'),
    path('<uuid:session_id>/', views.session_page, name='session'),
    path('<uuid:session_id>/status/', views.status, name='status'),
    path('<uuid:session_id>/<str:command>/', views.action, name='action'),
]
