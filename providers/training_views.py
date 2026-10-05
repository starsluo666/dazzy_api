from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .training import complete_training_lesson, submit_training_answers, training_payload
from .views import current_approved_provider


class TrainingVersionSerializer(serializers.Serializer):
    version_id = serializers.IntegerField(min_value=1)


class TrainingAnswerSerializer(TrainingVersionSerializer):
    answers = serializers.DictField(child=serializers.JSONField())


class ProviderTrainingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"data": training_payload(current_approved_provider(request))})


class ProviderTrainingLessonView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, lesson_id):
        provider = current_approved_provider(request)
        serializer = TrainingVersionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({"data": complete_training_lesson(
            provider=provider, lesson_id=lesson_id, **serializer.validated_data,
        )})


class ProviderTrainingSubmitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        provider = current_approved_provider(request)
        serializer = TrainingAnswerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({"data": submit_training_answers(provider=provider, **serializer.validated_data)})
