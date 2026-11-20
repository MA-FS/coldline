"""ColdLine web layer: design system, Jinja components, page templates and the fixture preview router.

Public surface used by the application routes:

* :data:`coldline.web.render.templates` - the configured ``Jinja2Templates`` instance (filters + globals).
* :func:`coldline.web.render.base_context` - the context every page needs (nav, clock, disclaimer, mode).
* :func:`coldline.web.views.plant_view` - turns a :class:`~coldline.contracts.PlantSnapshot` into the mimic
  view model.
* :mod:`coldline.web.preview` - fixture-backed ``/preview/*`` pages and ``/styleguide``.
"""
