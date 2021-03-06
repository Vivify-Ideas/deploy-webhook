#! /usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright © 2019 Pavle Portic <pavle.portic@tilda.center>
#
# Distributed under terms of the BSD-3-Clause license.

import docker
import time
from datetime import datetime
from sqlalchemy.exc import IntegrityError

from app.db import db
from app.models.service import Service
from .docker_controller import DockerController
from .exceptions import ServiceUpdateError


class ServiceController(DockerController):
	def __init__(self, controller=None):
		if controller is None:
			super(ServiceController, self).__init__()
		else:
			self.client = controller.client

	def get_services(self):
		return Service.query.all()

	def add_service(self, name, repository, tag):
		try:
			service = Service(name=name, repository=repository, tag=tag)
			db.session.add(service)
			db.session.commit()
			return service
		except IntegrityError:
			db.session.rollback()
			return None

	def delete_service(self, name):
		service = Service.query.filter_by(name=name).first()
		if service is None:
			return False

		db.session.delete(service)
		db.session.commit()
		return True

	def get_image_mappings(self, active_services, services_to_update):
		services = self.get_services()
		service_names = [service.name for service in active_services]
		self.image_mappings = {service.name: {
			'repository': service.repository,
			'tag': service.tag,
		} for service in services if service.name in service_names and service.name in services_to_update}

	def set_services_status(self, services):
		active_services = self.client.services.list()
		for service in services:
			if [active_services for active_service in active_services if active_service.name == service['name']]:
				service['active'] = True
			else:
				service['active'] = False

	def backup_images(self):
		try:
			for service in self.image_mappings:
				image_name = self.image_mappings[service]
				image = self.client.images.get(f'{image_name["repository"]}:{image_name["tag"]}')
				image.tag(image_name['repository'], tag='previous')
		except docker.errors.ImageNotFound as e:
			print(e)
			return False

		return True

	def pull_images(self):
		try:
			for service in self.image_mappings:
				image = self.image_mappings[service]
				self.client.images.pull(repository=image['repository'], tag=image['tag'])
		except docker.errors.NotFound:
			return False

		return True

	def wait_for_service_update(self, service, now):
		while True:
			time.sleep(1)
			service.reload()
			updated_at = service.attrs['UpdatedAt'].split('.')[0]
			updated_at = datetime.strptime(updated_at, '%Y-%m-%dT%H:%M:%S')
			state = service.attrs['UpdateStatus']['State']
			if updated_at > now and state != 'updating':
				return

	def update_service(self, service, image):
		now = datetime.utcnow()
		service.update(image=image, force_update=True)
		self.wait_for_service_update(service, now)
		update_state = service.attrs['UpdateStatus']['State']
		if update_state != 'completed':
			raise ServiceUpdateError(f'Failed to update service {service.name}')

	def update_stack(self, services_to_update):
		active_services = self.client.services.list()
		self.get_image_mappings(active_services, services_to_update)
		revert = self.backup_images()
		self.pull_images()
		updated_services = []

		for service in active_services:
			if service.name not in self.image_mappings:
				continue

			image = self.image_mappings[service.name]
			image = f'{image["repository"]}:{image["tag"]}'
			try:
				self.update_service(service, image)
				updated_services.append(service)
			except (docker.errors.APIError, ServiceUpdateError) as e:
				print(e)
				updated_services.append(service)
				if revert:
					return self.revert_services(updated_services, service.name)
				else:
					return {'err': 'Stack revert failed', 'msg': 'No backup images were created'}, 500

		return {'msg': 'Successfully updated all services'}, 200

	def revert_services(self, updated_services, failed_service):
		for service in updated_services:
			image = f'{self.image_mappings[service.name]["repository"]}:previous'
			try:
				self.update_service(service, image)
			except (docker.errors.APIError, ServiceUpdateError) as e:
				print(e)
				return {'err': 'Stack revert failed', 'msg': str(e)}, 500

		return {'err': 'Stack update failed', 'msg': f'Service {failed_service} failed to update. Stack reverted'}, 500
