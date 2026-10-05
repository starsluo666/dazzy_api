"""One-time first-order training. Draft edits never change a live examination."""

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import APIException, ValidationError

from .models import (
    ProviderProfile, ProviderTrainingAttempt, ProviderTrainingConfig,
    ProviderTrainingProgress, ProviderTrainingVersion,
)


class TrainingVersionChanged(APIException):
    status_code = 409
    default_detail = "学习资料已更新，请刷新页面后重新学习和答题。"
    default_code = "training_version_changed"


class LessonSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    title = serializers.CharField(max_length=80)
    content = serializers.CharField(max_length=10000)


class QuestionSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    title = serializers.CharField(max_length=500)
    options = serializers.ListField(child=serializers.CharField(max_length=300), min_length=2, max_length=6)
    correct_index = serializers.IntegerField(min_value=0, max_value=5)
    explanation = serializers.CharField(max_length=1000, allow_blank=True, default="")

    def validate(self, attrs):
        if attrs["correct_index"] >= len(attrs["options"]):
            raise ValidationError("正确答案必须对应一个有效选项。")
        if len(set(attrs["options"])) != len(attrs["options"]):
            raise ValidationError("同一题的选项不能重复。")
        return attrs


class CurriculumSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=80)
    pass_score = serializers.IntegerField(min_value=1, max_value=100, default=100)
    lessons = LessonSerializer(many=True, allow_empty=True, max_length=30)
    questions = QuestionSerializer(many=True, allow_empty=True, max_length=50)

    def validate(self, attrs):
        for key in ("lessons", "questions"):
            ids = [item["id"] for item in attrs[key]]
            if len(set(ids)) != len(ids):
                raise ValidationError({key: "资料或题目的编号不能重复。"})
        return attrs


def training_required(provider):
    return not (provider.training_exempt or provider.training_passed_at)


def require_training(provider):
    if training_required(provider):
        raise ValidationError({"training": "首次接单前，请先完成接单学习并通过考核。"})


def training_summary(provider):
    return {
        "required": training_required(provider),
        "passed": bool(provider.training_passed_at),
        "exempt": provider.training_exempt,
        "passed_at": provider.training_passed_at,
    }


def training_payload(provider):
    result = training_summary(provider)
    config = ProviderTrainingConfig.objects.select_related("active_version").filter(pk=1).first()
    version = config.active_version if config else None
    result.update({"course": None, "completed_lesson_ids": [], "last_result": None})
    if not version:
        return result
    progress = ProviderTrainingProgress.objects.filter(provider=provider, version=version).first()
    payload = version.payload
    # Explicit allow-list: never serialize correct_index or explanations to the learner.
    result["course"] = {
        "version_id": version.pk, "title": payload["title"], "pass_score": payload["pass_score"],
        "lessons": payload["lessons"],
        "questions": [{"id": q["id"], "title": q["title"], "options": q["options"]} for q in payload["questions"]],
    }
    result["completed_lesson_ids"] = progress.completed_lesson_ids if progress else []
    attempt = ProviderTrainingAttempt.objects.filter(provider=provider, version=version).first()
    if attempt:
        result["last_result"] = {"score": attempt.score, "passed": attempt.passed}
    return result


def _locked_version(version_id):
    config = ProviderTrainingConfig.objects.select_for_update().filter(pk=1).first()
    if not config or not config.active_version_id:
        raise ValidationError({"training": "平台尚未发布接单学习资料，请联系客服。"})
    if config.active_version_id != version_id:
        raise TrainingVersionChanged()
    return ProviderTrainingVersion.objects.get(pk=version_id)


@transaction.atomic
def complete_training_lesson(*, provider, version_id, lesson_id):
    locked = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
    version = _locked_version(version_id)
    lesson_id = str(lesson_id)
    if lesson_id not in {item["id"] for item in version.payload["lessons"]}:
        raise ValidationError({"lesson_id": "学习资料不存在，请刷新。"})
    progress, _ = ProviderTrainingProgress.objects.get_or_create(provider=locked, version=version)
    if lesson_id not in progress.completed_lesson_ids:
        progress.completed_lesson_ids = [*progress.completed_lesson_ids, lesson_id]
        progress.save(update_fields=("completed_lesson_ids", "updated_at"))
    return training_payload(locked)


@transaction.atomic
def submit_training_answers(*, provider, version_id, answers):
    locked = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
    if not training_required(locked):
        return training_payload(locked)
    version = _locked_version(version_id)
    progress = ProviderTrainingProgress.objects.filter(provider=locked, version=version).first()
    lesson_ids = {item["id"] for item in version.payload["lessons"]}
    if not progress or not lesson_ids.issubset(set(progress.completed_lesson_ids)):
        raise ValidationError({"training": "请先逐篇学习并确认完成全部学习资料。"})
    questions = version.payload["questions"]
    if not questions or set(answers) != {q["id"] for q in questions}:
        raise ValidationError({"answers": "请完成全部题目后提交。"})
    correct = 0
    for question in questions:
        answer = answers[question["id"]]
        if type(answer) is not int or not 0 <= answer < len(question["options"]):
            raise ValidationError({"answers": "包含无效答案，请重新作答。"})
        correct += answer == question["correct_index"]
    # Floor score; compare integers to avoid rounding a near-pass up to a pass.
    score = correct * 100 // len(questions)
    passed = correct * 100 >= version.payload["pass_score"] * len(questions)
    ProviderTrainingAttempt.objects.create(provider=locked, version=version, answers=answers, score=score, passed=passed)
    if passed:
        locked.training_passed_at = timezone.now()
        locked.save(update_fields=("training_passed_at", "updated_at"))
    return training_payload(locked)
