# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2017-06-20 20:18
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('crowdsourcing', '0180_merge'),
    ]

    operations = [
        migrations.AddField(
            model_name='workerbonus',
            name='charge',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, to='crowdsourcing.StripeCharge'),
        ),
    ]