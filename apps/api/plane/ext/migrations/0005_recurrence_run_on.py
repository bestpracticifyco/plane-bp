# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("plane_ext", "0004_recurrence"),
    ]

    operations = [
        migrations.AddField(
            model_name="extrecurrencerule",
            name="run_on",
            field=models.CharField(choices=[("due", "due"), ("start", "start")], default="due", max_length=8),
        ),
    ]
