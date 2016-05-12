from crowdsourcing import models
from datetime import datetime
from rest_framework import serializers
from crowdsourcing.serializers.dynamic import DynamicFieldsModelSerializer
from crowdsourcing.serializers.template import TemplateSerializer
from crowdsourcing.serializers.task import TaskSerializer, TaskCommentSerializer
from rest_framework.exceptions import ValidationError
from crowdsourcing.serializers.requester import RequesterSerializer
from crowdsourcing.serializers.message import CommentSerializer
from crowdsourcing.utils import generate_random_id
from crowdsourcing.serializers.file import BatchFileSerializer
from mturk.tasks import mturk_update_status
from django.utils import timezone
import copy


class CategorySerializer(DynamicFieldsModelSerializer):
    class Meta:
        model = models.Category
        fields = ('id', 'name', 'parent')

    def update(self, instance, validated_data):
        instance.name = validated_data.get('name', instance.name)
        instance.parent = validated_data.get('parent', instance.parent)
        instance.save()
        return instance

    def delete(self, instance):
        instance.deleted = True
        instance.save()
        return instance


class ProjectSerializer(DynamicFieldsModelSerializer):
    deleted = serializers.BooleanField(read_only=True)
    template = TemplateSerializer(many=False, required=False)
    total_tasks = serializers.SerializerMethodField()
    file_id = serializers.IntegerField(write_only=True, allow_null=True, required=False)
    age = serializers.SerializerMethodField()
    has_comments = serializers.SerializerMethodField()
    available_tasks = serializers.SerializerMethodField()
    comments = serializers.SerializerMethodField()
    name = serializers.CharField(default='Untitled Project')
    status = serializers.IntegerField(default=1)
    owner = RequesterSerializer(fields=('alias', 'profile', 'id', 'user_id'), read_only=True)
    batch_files = BatchFileSerializer(many=True, read_only=True,
                                      fields=('id', 'name', 'size', 'column_headers', 'format', 'number_of_rows'))
    num_rows = serializers.IntegerField(write_only=True, allow_null=True, required=False)
    requester_rating = serializers.FloatField(read_only=True, required=False)
    raw_rating = serializers.IntegerField(read_only=True, required=False)
    deadline = serializers.DateTimeField(required=False)

    class Meta:
        model = models.Project
        fields = ('id', 'name', 'owner', 'description', 'status', 'repetition', 'deadline', 'timeout', 'template',
                  'batch_files', 'deleted', 'created_timestamp', 'last_updated', 'price', 'has_data_set',
                  'data_set_location', 'total_tasks', 'file_id', 'age', 'is_micro', 'is_prototype', 'task_time',
                  'allow_feedback', 'feedback_permissions', 'min_rating', 'has_comments',
                  'available_tasks', 'comments', 'num_rows', 'requester_rating', 'raw_rating', 'post_mturk',
                  'group_id')
        read_only_fields = (
            'created_timestamp', 'last_updated', 'deleted', 'owner', 'has_comments', 'available_tasks',
            'comments', 'template', 'group_id',)

    def create(self, with_defaults=True, **kwargs):
        template_initial = self.validated_data.pop('template') if 'template' in self.validated_data else None
        template_items = template_initial['template_items'] if template_initial else []

        project = models.Project.objects.create(deleted=False, owner=kwargs['owner'].requester, **self.validated_data)
        project.group_id = project.id
        template = {
            "name": 't_' + generate_random_id(),
            "template_items": template_items
        }
        template_serializer = TemplateSerializer(data=template)
        if template_serializer.is_valid():
            template = template_serializer.create(with_defaults=with_defaults, owner=kwargs['owner'])
            project.template = template
        else:
            raise ValidationError(template_serializer.errors)
        if not with_defaults:
            project.status = models.Project.STATUS_IN_PROGRESS
            project.published_time = timezone.now()
            self.create_task(project.id)
        project.save()
        return project

    def create_revision(self, new_data, *args, **kwargs):
        revision = self.instance
        revision.pk = None
        # revision.repetition = new_data.get('repetition', revision.repetition)
        # revision.price = new_data.get('repetition', revision.price)
        # revision.deadline = new_data.get('repetition', revision.deadline)
        revision.published_time = timezone.now()
        revision.save()

    def delete(self, instance):
        instance.deleted = True
        instance.save()
        return instance

    @staticmethod
    def get_age(model):
        from crowdsourcing.utils import get_time_delta

        if model.status == models.Project.STATUS_SAVED:
            return "Saved " + get_time_delta(model.last_updated)
        else:
            return "Posted " + get_time_delta(model.published_time)

    @staticmethod
    def get_total_tasks(obj):
        return obj.project_tasks.all().count()

    @staticmethod
    def get_has_comments(obj):
        return obj.projectcomment_project.count() > 0

    def get_available_tasks(self, obj):
        available_task_count = models.Project.objects.values('id').raw('''
          SELECT count(*) id FROM (
            SELECT
              "crowdsourcing_task"."id"
            FROM "crowdsourcing_task"
              INNER JOIN "crowdsourcing_project" ON ("crowdsourcing_task"."project_id" = "crowdsourcing_project"."id")
              LEFT OUTER JOIN "crowdsourcing_taskworker" ON ("crowdsourcing_task"."id" =
                "crowdsourcing_taskworker"."task_id" AND task_status NOT IN (4,6,7))
            WHERE ("crowdsourcing_task"."project_id" = %s AND NOT (
              ("crowdsourcing_task"."id" IN (SELECT U1."task_id" AS Col1
              FROM "crowdsourcing_taskworker" U1 WHERE U1."worker_id" = %s AND U1.task_status<>6))))
            GROUP BY "crowdsourcing_task"."id", "crowdsourcing_project"."repetition"
            HAVING "crowdsourcing_project"."repetition" > (COUNT("crowdsourcing_taskworker"."id"))) available_tasks
            ''', params=[obj.id, self.context['request'].user.userprofile.worker.id])[0].id
        return available_task_count

    @staticmethod
    def get_comments(obj):
        if obj:
            comments = []
            tasks = obj.project_tasks.all()
            for task in tasks:
                task_comments = task.taskcomment_task.all()
                for task_comment in task_comments:
                    comments.append(task_comment)
            serializer = TaskCommentSerializer(many=True, instance=comments, read_only=True)
            return serializer.data
        return []

    def update(self, *args, **kwargs):
        status = self.validated_data.get('status', self.instance.status)
        # self.create_revision(None, *args, **kwargs)
        num_rows = self.validated_data.get('num_rows', 0)
        if self.instance.status != status and status == models.Project.STATUS_PUBLISHED:
            if self.instance.template.template_items.count() == 0:
                raise ValidationError('At least one template item is required')
            if self.instance.batch_files.count() == 0:
                self.create_task(self.instance.id)
            else:
                batch_file = self.instance.batch_files.first()
                data = batch_file.parse_csv()
                count = 0
                for row in data:
                    if count == num_rows:
                        break
                    task = {
                        'project': self.instance.id,
                        'data': row
                    }
                    task_serializer = TaskSerializer(data=task)
                    if task_serializer.is_valid():
                        task_serializer.create(**kwargs)
                        count += 1
                    else:
                        raise ValidationError(task_serializer.errors)
            self.instance.published_time = datetime.now()
            status = models.Project.STATUS_IN_PROGRESS

        self.instance.name = self.validated_data.get('name', self.instance.name)
        self.instance.price = self.validated_data.get('price', self.instance.price)
        self.instance.repetition = self.validated_data.get('repetition', self.instance.repetition)
        self.instance.deadline = self.validated_data.get('deadline', self.instance.deadline)
        self.instance.timeout = self.validated_data.get('timeout', self.instance.timeout)
        self.instance.post_mturk = self.validated_data.get('post_mturk', self.instance.post_mturk)
        if status != self.instance.status \
            and status in (models.Project.STATUS_PAUSED, models.Project.STATUS_IN_PROGRESS) and \
                self.instance.status in (models.Project.STATUS_PAUSED, models.Project.STATUS_IN_PROGRESS):
            mturk_update_status.delay({'id': self.instance.id, 'status': status})
        self.instance.status = status
        self.instance.save()

        return self.instance

    @staticmethod
    def create_task(project_id):
        task_data = {
            "project": project_id,
            "status": 1,
            "data": {}
        }
        task_serializer = TaskSerializer(data=task_data)
        if task_serializer.is_valid():
            task_serializer.create()
        else:
            raise ValidationError(task_serializer.errors)

    def fork(self, *args, **kwargs):
        template = self.instance.template
        template_items = copy.copy(template.template_items.all())
        categories = self.instance.categories.all()
        batch_files = self.instance.batch_files.all()

        project = self.instance
        project.name += ' (copy)'
        project.status = 1
        project.is_prototype = False
        project.parent = models.Project.objects.get(pk=self.instance.id)
        template.pk = None
        template.save()
        project.template = template

        for template_item in template_items:
            template_item.pk = None
            template_item.template = template
            template_item.save()
        project.id = None
        project.save()

        for category in categories:
            project_category = models.ProjectCategory(project=project, category=category)
            project_category.save()
        for batch_file in batch_files:
            project_batch_file = models.ProjectBatchFile(project=project, batch_file=batch_file)
            project_batch_file.save()


class QualificationApplicationSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Qualification


class QualificationItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.QualificationItem


class ProjectCommentSerializer(DynamicFieldsModelSerializer):
    comment = CommentSerializer()

    class Meta:
        model = models.ProjectComment
        fields = ('id', 'project', 'comment')
        read_only_fields = ('project',)

    def create(self, **kwargs):
        comment_data = self.validated_data.pop('comment')
        comment_serializer = CommentSerializer(data=comment_data)
        if comment_serializer.is_valid():
            comment = comment_serializer.create(sender=kwargs['sender'])
            project_comment = models.ProjectComment.objects.create(project_id=kwargs['project'], comment_id=comment.id)
            return {'id': project_comment.id, 'comment': comment}


class ProjectBatchFileSerializer(DynamicFieldsModelSerializer):
    class Meta:
        model = models.ProjectBatchFile
        fields = ('id', 'project', 'batch_file')
        read_only_fields = ('project',)

    def create(self, project=None, **kwargs):
        project_file = models.ProjectBatchFile.objects.create(project_id=project, **self.validated_data)
        return project_file
