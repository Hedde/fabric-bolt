"""
Views for the Projects App
"""

import datetime
import subprocess
import os
import re
import sys

from django.http import HttpResponseRedirect, StreamingHttpResponse
from django.db.models.aggregates import Count
from django.contrib import messages
from django.views.generic import CreateView, UpdateView, DetailView, DeleteView, RedirectView, View
from django.core.urlresolvers import reverse_lazy, reverse
from django.shortcuts import get_object_or_404
from django.forms import CharField, PasswordInput, Select, FloatField, BooleanField
from django.conf import settings
from django.utils.text import slugify
from git import Repo
from django_tables2 import RequestConfig, SingleTableView

from fabric_bolt.core.mixins.views import MultipleGroupRequiredMixin
from fabric_bolt.hosts.models import Host
from fabric_bolt.projects import forms, tables, models





# These options are passed to Fabric as: fab task --abort-on-prompts=True --user=root ...
fabric_special_options = ['no_agent', 'forward-agent', 'config', 'disable-known-hosts', 'keepalive',
                          'password', 'parallel', 'no-pty', 'reject-unknown-hosts', 'skip-bad-hosts', 'timeout',
                          'command-timeout', 'user', 'warn-only', 'pool-size']


def get_fabfile_path(project):
    if project.use_repo_fabfile:
        repo_dir = os.path.join(settings.PUBLIC_DIR, '.repo_caches', slugify(project.name))
        if not os.path.exists(repo_dir):
            os.makedirs(repo_dir)
            Repo.clone_from(project.repo_url, repo_dir) # we may want to do a git pull if it already exists?

        pip_installs = ' '.join(project.fabfile_requirements.splitlines())
        subprocess.call(['pip install {}'.format(pip_installs), '--target {}'.format(repo_dir)], shell=True)

        fabfile_path = os.path.join(repo_dir, 'fabfile.py')
    else:
        fabfile_path = settings.FABFILE_PATH

    return fabfile_path


def get_fabric_tasks(request, project):
    """
    Generate a list of fabric tasks that are available
    """
    try:
        fabfile_path = get_fabfile_path(project)

        output = subprocess.check_output(['fab', '--list', '--fabfile={}'.format(fabfile_path)])
        lines = output.splitlines()[2:]
        dict_with_docs = {}
        for line in lines:
            match = re.match(r'^\s*([^\s]+)\s*(.*)$', line)
            if match:
                name, desc = match.group(1), match.group(2)
                if desc.endswith('...'):
                    o = subprocess.check_output(['fab', '--display={}'.format(name), '--fabfile={}'.format(fabfile_path)])
                    try:
                        desc = o.splitlines()[2].strip()
                    except:
                        pass # just stick with the original truncated description
                dict_with_docs[name] = desc
    except Exception as e:
        messages.error(request, 'Error loading fabfile: ' + str(e))
        dict_with_docs = {}
    return dict_with_docs


class BaseGetProjectCreateView(CreateView):
    """
    Reusable class for create views that need the project pulled in
    """

    def dispatch(self, request, *args, **kwargs):

        # Lets set the project so we can use it later
        project_id = kwargs.get('project_id')
        self.project = models.Project.objects.get(pk=project_id)

        return super(BaseGetProjectCreateView, self).dispatch(request, *args, **kwargs)


class ProjectList(SingleTableView):
    """
    Project List page
    """

    table_class = tables.ProjectTable
    model = models.Project
    queryset = models.Project.active_records.all()


class ProjectCreate(MultipleGroupRequiredMixin, CreateView):
    """
    Create a new project
    """
    group_required = ['Admin', 'Deployer', ]
    model = models.Project
    form_class = forms.ProjectCreateForm
    template_name_suffix = '_create'

    def form_valid(self, form):
        """After the form is valid lets let people know"""

        ret = super(ProjectCreate, self).form_valid(form)

        # Good to make note of that
        messages.add_message(self.request, messages.SUCCESS, 'Project %s created' % self.object.name)

        return ret


class ProjectDetail(DetailView):
    """
    Display the Project Detail/Summary page: Configurations, Stages, and Deployments
    """

    model = models.Project

    def dispatch(self, request, *args, **kwargs):
        if request.user.user_is_historian():
            self.template_name = "projects/historian_detail.html"

        return super(ProjectDetail, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(ProjectDetail, self).get_context_data(**kwargs)

        configuration_table = tables.ConfigurationTable(self.object.project_configurations(), prefix='config_')
        RequestConfig(self.request).configure(configuration_table)
        context['configurations'] = configuration_table

        stages = self.object.get_stages().annotate(deployment_count=Count('deployment'))
        context['stages'] = stages

        stage_table = tables.StageTable(stages, prefix='stage_')
        RequestConfig(self.request).configure(stage_table)
        context['stage_table'] = stage_table

        deployment_table = tables.DeploymentTable(models.Deployment.objects.filter(stage__in=stages).select_related('stage', 'task'), prefix='deploy_')
        RequestConfig(self.request).configure(deployment_table)
        context['deployment_table'] = deployment_table

        return context


class ProjectUpdate(MultipleGroupRequiredMixin, UpdateView):
    """
    Update a project
    """
    group_required = ['Admin', 'Deployer', ]
    model = models.Project
    form_class = forms.ProjectUpdateForm
    template_name_suffix = '_update'
    success_url = reverse_lazy('projects_project_list')


class ProjectDelete(MultipleGroupRequiredMixin, DeleteView):
    """
    Deletes a project by setting the Project's date_deleted. We save projects for historical tracking.
    """
    group_required = ['Admin', ]
    model = models.Project

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.object.date_deleted = datetime.datetime.now()
        self.object.save()

        messages.add_message(request, messages.WARNING, 'Project {} Successfully Deleted'.format(self.object))
        return HttpResponseRedirect(reverse('projects_project_list'))


class ProjectConfigurationCreate(MultipleGroupRequiredMixin, BaseGetProjectCreateView):
    """
    Create a Project Configuration. These are used to set the Fabric env object for a task.
    """
    group_required = ['Admin', ]
    model = models.Configuration
    template_name_suffix = '_create'
    form_class = forms.ConfigurationCreateForm

    def form_valid(self, form):
        """Set the project on this configuration after it's valid"""

        self.object = form.save(commit=False)
        self.object.project = self.project

        if self.kwargs.get('stage_id', None):
            current_stage = models.Stage.objects.get(pk=self.kwargs.get('stage_id'))
            self.object.stage = current_stage

        self.object.save()

        # Good to make note of that
        messages.add_message(self.request, messages.SUCCESS, 'Configuration %s created' % self.object.key)

        return super(ProjectConfigurationCreate, self).form_valid(form)

    def get_success_url(self):
        success_url = super(ProjectConfigurationCreate, self).get_success_url()

        if self.object.stage:
            success_url = reverse('projects_stage_view', args=(self.object.pk, self.object.stage.pk))

        return success_url


class ProjectConfigurationUpdate(MultipleGroupRequiredMixin, UpdateView):
    """
    Update a Project Configuration
    """
    group_required = ['Admin', ]
    model = models.Configuration
    template_name_suffix = '_update'
    form_class = forms.ConfigurationUpdateForm


class ProjectConfigurationDelete(MultipleGroupRequiredMixin, DeleteView):
    """
    Delete a project configuration from a project
    """
    group_required = ['Admin', ]
    model = models.Configuration

    def dispatch(self, request, *args, **kwargs):

        return super(ProjectConfigurationDelete, self).dispatch(request, *args, **kwargs)

    def get_success_url(self):
        """Get the url depending on what type of configuration I deleted."""
        
        if self.stage_id:
            url = reverse('projects_stage_view', args=(self.project_id, self.stage_id))
        else:
            url = reverse('projects_project_view', args=(self.project_id,))

        return url

    def delete(self, request, *args, **kwargs):

        obj = self.get_object()

        # Save where I was before I go and delete myself
        self.project_id = obj.project.pk
        self.stage_id = obj.stage.pk if obj.stage else None

        messages.success(self.request, 'Configuration {} Successfully Deleted'.format(self.get_object()))
        return super(ProjectConfigurationDelete, self).delete(self, request, *args, **kwargs)


class DeploymentCreate(MultipleGroupRequiredMixin, CreateView):
    """
    Form to create a new Deployment for a Project Stage. POST will kick off the DeploymentOutputStream view.
    """
    group_required = ['Admin', 'Deployer', ]
    model = models.Deployment
    form_class = forms.DeploymentForm

    def dispatch(self, request, *args, **kwargs):
        #save the stage for later
        self.stage = get_object_or_404(models.Stage, pk=int(kwargs['pk']))

        all_tasks = get_fabric_tasks(self.request, self.stage.project)
        if self.kwargs['task_name'] not in all_tasks:
            messages.error(self.request, '"{}" is not a valid task.'. format(self.kwargs['task_name']))
            return HttpResponseRedirect(reverse('projects_stage_view', kwargs={'project_id': self.stage.project_id, 'pk': self.stage.pk }))

        self.task_name = self.kwargs['task_name']
        self.task_description = all_tasks.get(self.task_name, None)

        return super(DeploymentCreate, self).dispatch(request, *args, **kwargs)

    def get_form(self, form_class):

        stage_configurations = self.stage.get_queryset_configurations(prompt_me_for_input=True)

        form = form_class(**self.get_form_kwargs())

        # We want to inject fields into the form for the configurations they've marked as prompt
        for config in stage_configurations:
            str_config_key = 'configuration_value_for_{}'.format(config.key)

            if config.data_type == config.BOOLEAN_TYPE:
                form.fields[str_config_key] = BooleanField(widget=Select(choices=((False, 'False'), (True, 'True'))))
                form.fields[str_config_key].coerce=lambda x: x == 'True',
            elif config.data_type == config.NUMBER_TYPE:
                form.fields[str_config_key] = FloatField()
            else:
                if config.sensitive_value:
                    form.fields[str_config_key] = CharField(widget=PasswordInput)
                else:
                    form.fields[str_config_key] = CharField()

            form.helper.layout.fields.insert(len(form.helper.layout.fields)-1, str_config_key)

        return form

    def form_valid(self, form):
        self.object = form.save(commit=False)
        self.object.stage = self.stage

        self.object.task, created = models.Task.objects.get_or_create(name=self.task_name, defaults={'description': self.task_description})
        if not created:
            self.object.task.times_used += 1
            self.object.task.description = self.task_description
            self.object.task.save()

        self.object.user = self.request.user
        self.object.save()

        configuration_values = {}
        for key, value in form.cleaned_data.iteritems():
            if key.startswith('configuration_value_for_'):
                configuration_values[key.replace('configuration_value_for_', '')] = value

        self.request.session['configuration_values'] = configuration_values

        return super(DeploymentCreate, self).form_valid(form)

    def get_context_data(self, **kwargs):
        context = super(DeploymentCreate, self).get_context_data(**kwargs)

        context['configs'] = self.stage.get_queryset_configurations(prompt_me_for_input=False)
        context['stage'] = self.stage
        context['task_name'] = self.task_name
        context['task_description'] = self.task_description
        return context

    def get_success_url(self):
        return reverse('projects_deployment_detail', kwargs={'pk': self.object.pk})


class DeploymentDetail(DetailView):
    """
    Display the detail/summary of a deployment
    """
    model = models.Deployment

    def get_template_names(self):
        if getattr(settings, 'SOCKETIO_ENABLED', False):
            return ['projects/deployment_detail_socketio.html']
        else:
            return ['projects/deployment_detail.html']


class DeploymentOutputStream(View):
    """
    Deployment view does the heavy lifting of calling Fabric Task for a Project Stage
    """

    def build_command(self):
        command = getattr(settings, 'VENV_PATH', '') + 'fab ' + '-f {} '.format(
            settings.FABFILE_PATH
        ) + self.object.task.name + ' --abort-on-prompts'

        hosts = self.object.stage.hosts.values_list('name', flat=True)
        if hosts:
            command.append('--hosts=' + ','.join(hosts))

        # Get the dictionary of configurations for this stage
        config = self.object.stage.get_configurations()

        config.update(self.request.session.get('configuration_values', {}))

        command_to_config = {x.replace('-', '_'): x for x in fabric_special_options}

        # Take the special env variables out
        normal_options = list(set(config.keys()) - set(command_to_config.keys()))

        # Special ones get set a different way
        special_options = list(set(config.keys()) & set(command_to_config.keys()))

        def get_key_value_string(key, value):
            if isinstance(value, bool):
                return key + ('' if value else '=')
            elif isinstance(value, float):
                return key + '=' + str(value)
            else:
                return '{}={}'.format(key, value.replace('"', '\\"'))

        if normal_options:
            command.append('--set')
            command.append(','.join(get_key_value_string(key, config[key]) for key in normal_options))

        if special_options:
            for key in special_options:
                command.append('--' + get_key_value_string(command_to_config[key], config[key]))

        command.append('--fabfile={}'.format(get_fabfile_path(self.object.stage.project)))

        print command

        return command

    def output_stream_generator(self):
        if self.object.task.name not in get_fabric_tasks(self.request, self.object.stage.project):
            return

        try:
            process = subprocess.Popen(self.build_command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            all_output = ''
            while True:
                nextline = process.stdout.readline()
                if nextline == '' and process.poll() != None:
                    break

                all_output += nextline

                yield '<span style="color:rgb(200, 200, 200);font-size: 14px;font-family: \'Helvetica Neue\', Helvetica, Arial, sans-serif;">{} </span><br /> {}'.format(nextline, ' '*1024)
                sys.stdout.flush()

            self.object.status = self.object.SUCCESS if process.returncode == 0 else self.object.FAILED

            yield '<span id="finished" style="display:none;">{}</span> {}'.format(self.object.status, ' '*1024)

            self.object.output = all_output
            self.object.save()

        except Exception as e:
            message = "An error occurred: " + e.message
            yield '<span style="color:rgb(200, 200, 200);font-size: 14px;font-family: \'Helvetica Neue\', Helvetica, Arial, sans-serif;">{} </span><br /> {}'.format(message, ' '*1024)
            yield '<span id="finished" style="display:none;">failed</span> {}'.format('*1024')

    def get(self, request, *args, **kwargs):
        self.object = get_object_or_404(models.Deployment, pk=int(kwargs['pk']), status=models.Deployment.PENDING)
        resp = StreamingHttpResponse(self.output_stream_generator())
        return resp


class ProjectStageCreate(MultipleGroupRequiredMixin, BaseGetProjectCreateView):
    """
    Create/Add a Stage to a Project
    """
    group_required = ['Admin', ]
    model = models.Stage
    template_name_suffix = '_create'
    form_class = forms.StageCreateForm

    def form_valid(self, form):
        """Set the project on this configuration after it's valid"""

        self.object = form.save(commit=False)
        self.object.project = self.project
        self.object.save()

        # Good to make note of that
        messages.add_message(self.request, messages.SUCCESS, 'Stage %s created' % self.object.name)

        return super(ProjectStageCreate, self).form_valid(form)


class ProjectStageUpdate(MultipleGroupRequiredMixin, UpdateView):
    """
    Project Stage Update form
    """
    group_required = ['Admin', 'Deployer', ]
    model = models.Stage
    template_name_suffix = '_update'
    form_class = forms.StageUpdateForm


class ProjectStageView(DetailView):
    """
    Display the details on a project stage: List Hosts, Configurations, and Tasks available to run
    """

    model = models.Stage

    def get_context_data(self, **kwargs):

        context = super(ProjectStageView, self).get_context_data(**kwargs)

        # Hosts Table (Stage->Host Through table)
        stage_hosts = self.object.hosts.all()

        host_table = tables.StageHostTable(stage_hosts, stage_id=self.object.pk)  # Through table
        RequestConfig(self.request).configure(host_table)
        context['hosts'] = host_table

        context['available_hosts'] = Host.objects.exclude(id__in=[host.pk for host in stage_hosts]).all()

        # Configuration Table
        configuration_table = tables.ConfigurationTable(self.object.stage_configurations())
        RequestConfig(self.request).configure(configuration_table)
        context['configurations'] = configuration_table

        #deployment table
        deployment_table = tables.DeploymentTable(models.Deployment.objects.filter(stage=self.object).select_related('stage', 'task'), prefix='deploy_')
        RequestConfig(self.request).configure(deployment_table)
        context['deployment_table'] = deployment_table

        return context


class ProjectStageTasksAjax(DetailView):
    model = models.Stage
    template_name = 'projects/stage_tasks_snippet.html'

    def get_context_data(self, **kwargs):
        context = super(ProjectStageTasksAjax, self).get_context_data(**kwargs)

        all_tasks = get_fabric_tasks(self.request, self.object.project)

        context['all_tasks'] = all_tasks.keys()
        context['frequent_tasks_run'] = models.Task.objects.filter(name__in=all_tasks.keys()).order_by('-times_used')[:3]

        return context


class ProjectStageDelete(MultipleGroupRequiredMixin, DeleteView):
    """
    Delete a project stage
    """
    group_required = ['Admin', ]
    model = models.Stage

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.object.date_deleted = datetime.datetime.now()
        self.object.save()

        messages.add_message(request, messages.WARNING, 'Stage {} Successfully Deleted'.format(self.object))
        return HttpResponseRedirect(reverse('projects_project_view', args=(self.object.project.pk,)))


class ProjectStageMapHost(MultipleGroupRequiredMixin, RedirectView):
    """
    Map a Project Stage to a Host
    """
    group_required = ['Admin',]
    permanent = False

    def get(self, request, *args, **kwargs):
        self.project_id = kwargs.get('project_id')
        self.stage_id = kwargs.get('pk')
        host_id = kwargs.get('host_id')

        stage = models.Stage.objects.get(pk=self.stage_id)
        stage.hosts.add(Host.objects.get(pk=host_id))

        return super(ProjectStageMapHost, self).get(request, *args, **kwargs)

    def get_redirect_url(self, **kwargs):
        return reverse('projects_stage_view', args=(self.project_id, self.stage_id,))


class ProjectStageUnmapHost(MultipleGroupRequiredMixin, RedirectView):
    """
    Unmap a Project Stage from a Host (deletes the Stage->Host through table record)
    """
    group_required = ['Admin', ]
    permanent = False

    def get(self, request, *args, **kwargs):
        self.stage_id = kwargs.get('pk')
        host_id = kwargs.get('host_id')

        self.stage = models.Stage.objects.get(pk=self.stage_id)
        host = Host.objects.get(pk=int(host_id))
        self.stage.hosts.remove(host)

        return super(ProjectStageUnmapHost, self).get(request, *args, **kwargs)

    def get_redirect_url(self, **kwargs):
        return reverse('projects_stage_view', args=(self.stage.project.pk, self.stage_id,))
